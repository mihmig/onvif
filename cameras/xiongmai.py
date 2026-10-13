# Модуль работы с камерами Xiongmai (XM / чипы Xiongmai / GK7201 / H264).
#
# Проверено на IPC_GK7201V200_G3-WQ-G_S38 (прошивка V5.00.C01...ONVIF 21.06).
#
# Камера не совместима со стандартным ONVIF-стеком Hikvision:
#   * на 80 порту висит веб-сервер камеры, возвращающий HTML вместо SOAP;
#   * ONVIF-сервис у Xiongmai работает на порту 8899 (/onvif/media_service);
#   * HTTP-снимок (GetSnapshotUri / webcapture.jpg) отдаёт только 800×448,
#     тогда как основной RTSP-поток — 2304×1296 (H.265);
#   * RTSP-ссылка имеет собственный формат с логином и паролем в пути:
#       rtsp://<host>:<port>/user=<user>_password=<pass>_channel=0_stream=0&onvif=0.sdp?real_stream
#
# Поэтому класс наследуется напрямую от базового Camera и не зависит от ONVIF
# при снятии фото и записи видео: всё берётся прямо из полноразмерного RTSP-потока.
# PTZ работает через ONVIF (порт 8899 по умолчанию) с ленивой инициализацией.
import io
import logging
import os
import time
from urllib.parse import quote

import av
from onvif import ONVIFClient

from cameras.camera import Camera, CameraOffline
from cameras.menu import (
    keyboard_layout,
    make_keyboard,
    make_telegram_keyboard,
    ptz_directions,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Xiongmai",
    "keyboard_layout",
    "make_keyboard",
    "make_telegram_keyboard",
    "ptz_directions",
]


class Xiongmai(Camera):
    """Камера Xiongmai (XM).

    Поворотная камера с одним объективом. Снимки и видео берутся напрямую
    из RTSP-потока в полном разрешении без обращения к ONVIF на старте.
    """

    has_ptz: bool = True
    has_second_lens: bool = False

    default_rtsp_port: int = 554
    default_onvif_port: int = 8899
    default_channel: int = 0
    default_stream: int = 0  # 0 — основной поток 2304×1296, 1 — субпоток 800×448

    # Таймауты и повторные попытки подключения к RTSP (настраиваются в конфиге)
    default_stream_timeout: float = 25.0      # Секунды на открытие и чтение сокета
    default_stream_attempts: int = 3          # Количество попыток
    default_stream_retry_delay: float = 1.5   # Пауза между попытками для освобождения сессии камерой

    # PTZ: некоторые камеры Xiongmai инвертируют оси (лево/право, вверх/вниз).
    # Инверсию можно отключить в конфиге: `ptz_invert_x: false`, `ptz_invert_y: false`.
    default_ptz_invert_x: bool = True
    default_ptz_invert_y: bool = False

    def __init__(self, config: dict):
        super().__init__(config)
        self._onvif_client = None
        self._ptz_service = None
        self._ptz_profile_token = None
        self._enhancer = None

    def run(self):
        pass

    def _get_stream_uri(self, stream: int | None = None) -> str:
        """Собирает RTSP-ссылку для указанного потока камеры Xiongmai.

        Если в конфиге явно задан `rtsp_url`, используется он.
        Иначе собирается ссылка формата Xiongmai:
            rtsp://host:554/user=USER_password=PASS_channel=0_stream=0&onvif=0.sdp?real_stream
        """
        if self.config.get('rtsp_url'):
            return self.config['rtsp_url']

        host = self.config['host']
        port = self.config.get('rtsp_port', self.default_rtsp_port)
        user = quote(str(self.config.get('user', '')), safe='')
        password = quote(str(self.config.get('pass', '')), safe='')
        channel = self.config.get('channel', self.default_channel)

        if stream is None:
            stream = self.config.get('stream', self.default_stream)

        return (f"rtsp://{host}:{port}/user={user}_password={password}"
                f"_channel={channel}_stream={stream}&onvif=0.sdp?real_stream")

    @staticmethod
    def _encode_jpeg(frame: av.VideoFrame) -> bytes:
        """Кодирует видеокадр в JPEG средствами PyAV."""
        frame = frame.reformat(format='yuvj420p')
        frame.pts = None
        buf = io.BytesIO()
        with av.open(buf, mode='w', format='mjpeg') as output:
            stream = output.add_stream('mjpeg')
            stream.width = frame.width
            stream.height = frame.height
            stream.pix_fmt = 'yuvj420p'
            for packet in stream.encode(frame):
                output.mux(packet)
            for packet in stream.encode(None):
                output.mux(packet)
        return buf.getvalue()

    def _get_enhancer(self):
        """Улучшатель снимка из config (`enhance`), создаётся один раз. None — выключено."""
        if self._enhancer is None and self.config.get('enhance'):
            from cameras.enhance import make_enhancer
            self._enhancer = make_enhancer(self.config.get('enhance'))
        return self._enhancer

    def _stream_options(self) -> tuple[dict, float, int, float]:
        """Параметры RTSP-подключения: options, таймаут, число попыток, пауза между ними."""
        stream_timeout = float(self.config.get('stream_timeout', self.default_stream_timeout))
        attempts = int(self.config.get('stream_attempts', self.default_stream_attempts))
        retry_delay = float(self.config.get('stream_retry_delay', self.default_stream_retry_delay))

        options = {
            'rtsp_transport': self.config.get('rtsp_transport', 'tcp'),
            # stimeout — таймаут сокета ffmpeg в микросекундах
            'stimeout': str(int(stream_timeout * 1_000_000)),
        }
        return options, stream_timeout, attempts, retry_delay

    def _first_frame(self, source: str) -> av.VideoFrame:
        """Первый видеокадр RTSP-потока `source`.

        При медленном ответе или занятой сессии камеры делает до
        `stream_attempts` попыток с паузой `stream_retry_delay`.
        Если кадров нет — CameraOffline.
        """
        options, stream_timeout, attempts, retry_delay = self._stream_options()
        last_error = None

        for attempt in range(1, attempts + 1):
            try:
                with av.open(source, options=options,
                             timeout=(stream_timeout, stream_timeout)) as container:
                    return next(container.decode(video=0))
            except (av.error.FFmpegError, StopIteration) as error:
                last_error = error
                logger.warning("Попытка %s из %s получить кадр из RTSP не удалась: %s",
                               attempt, attempts, error)
                if attempt < attempts:
                    time.sleep(retry_delay)

        logger.error("Все %s попыток получить кадр из RTSP Xiongmai исчерпаны: %s", attempts, last_error)
        raise CameraOffline(f"камера {self.config['host']} не отдаёт видеопоток: {last_error}")

    def _snapshot_from_stream(self, source: str) -> bytes:
        """Декодирует первый кадр из RTSP-потока и возвращает JPEG."""
        frame = self._first_frame(source)

        enhancer = self._get_enhancer()
        if enhancer is None:
            return self._encode_jpeg(frame)

        import cv2
        bgr = enhancer(frame.to_ndarray(format='bgr24'))
        ok, buf = cv2.imencode('.jpg', bgr, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok:
            logger.warning("Не удалось закодировать улучшенный кадр, отдаём без обработки")
            return self._encode_jpeg(frame)
        return buf.tobytes()

    def get_image(self, profile_token: str | None = None) -> bytes:
        """Снимок основного потока камеры первым кадром из RTSP (полное разрешение)."""
        source = self._get_stream_uri()
        return self._snapshot_from_stream(source)

    def get_all_image(self, profile_token: str | None = None) -> bytes:
        """У камеры один объектив — отдаём основной снимок."""
        return self.get_image(profile_token)

    def _record_stream(self, source: str, duration: float, filename: str) -> str | None:
        """Записывает `duration` секунд RTSP-потока `source` в MP4 без перекодирования.

        При медленном ответе делает до `stream_attempts` попыток с таймаутом `stream_timeout`.
        """
        logger.info("Запись %s сек. в '%s'...", duration, filename)

        stream_timeout = float(self.config.get('stream_timeout', self.default_stream_timeout))
        attempts = int(self.config.get('stream_attempts', self.default_stream_attempts))
        retry_delay = float(self.config.get('stream_retry_delay', self.default_stream_retry_delay))

        options = {
            'rtsp_transport': self.config.get('rtsp_transport', 'tcp'),
            'stimeout': str(int(stream_timeout * 1_000_000)),
        }

        last_error = None
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            first_ts = None
            frames = 0

            try:
                with av.open(source, options=options, timeout=(stream_timeout, stream_timeout)) as source_container, \
                        av.open(filename, mode='w') as output:

                    in_stream = source_container.streams.video[0]
                    out_stream = output.add_stream_from_template(in_stream)

                    for packet in source_container.demux(in_stream):
                        if packet.dts is None:
                            continue

                        if first_ts is None:
                            if not packet.is_keyframe:
                                continue
                            first_ts = packet.dts

                        packet.dts -= first_ts
                        if packet.pts is not None:
                            packet.pts -= first_ts

                        elapsed = float(packet.dts * in_stream.time_base)

                        packet.stream = out_stream
                        output.mux(packet)
                        frames += 1

                        if elapsed >= duration:
                            break

                        if time.monotonic() - started > duration + stream_timeout + 10:
                            logger.warning("Превышено время ожидания потока, запись прервана.")
                            break

                if frames and os.path.exists(filename):
                    size_mb = os.path.getsize(filename) / (1024 * 1024)
                    logger.info("Видео успешно сохранено: '%s' (%s кадров, %s МБ)",
                                filename, frames, round(size_mb, 2))
                    return filename

                logger.warning("Попытка %s: не получено ни одного кадра, повторяем...", attempt)
            except av.error.FFmpegError as error:
                last_error = error
                logger.warning("Попытка %s из %s записи потока Xiongmai не удалась: %s",
                               attempt, attempts, error)
                if os.path.exists(filename):
                    try:
                        os.remove(filename)
                    except Exception:
                        pass

            if attempt < attempts:
                time.sleep(retry_delay)

        logger.error("Все %s попыток записи потока Xiongmai исчерпаны: %s", attempts, last_error)
        raise CameraOffline(f"камера {self.config['host']} не отдаёт видеопоток: {last_error}")

    def get_video(self, profile_token: str | None = None, duration: float = 3.0, filename: str = '') -> str | None:
        """Запись видеоролика заданной длительности из RTSP-потока в MP4."""
        source = self._get_stream_uri()
        return self._record_stream(source, duration, filename)

    # -------------------------------------------------------------------------
    # PTZ
    # -------------------------------------------------------------------------
    def _init_ptz(self) -> bool:
        """Ленивая инициализация PTZ через ONVIF (порт 8899 по умолчанию)."""
        if self._ptz_service is not None:
            return True

        # У Xiongmai ONVIF висит на 8899 (на 80 висит веб-интерфейс)
        onvif_port = self.config.get('onvif_port')
        if not onvif_port:
            port = self.config.get('port', self.default_onvif_port)
            onvif_port = self.default_onvif_port if port == 80 else port

        try:
            self._onvif_client = ONVIFClient(
                self.config['host'],
                onvif_port,
                self.config.get('user', ''),
                self.config.get('pass', ''),
            )
            media = self._onvif_client.media()
            profiles = media.GetProfiles()
            self._ptz_profile_token = profiles[0].token if profiles else '000'
            self._ptz_service = self._onvif_client.ptz()
            logger.info("PTZ Xiongmai инициализирован (порт %s, профиль %s)",
                        onvif_port, self._ptz_profile_token)
            return True
        except Exception as error:
            logger.warning("Не удалось инициализировать ONVIF PTZ на %s:%s: %s",
                           self.config['host'], onvif_port, error)
            self._onvif_client = None
            self._ptz_service = None
            return False

    def _ptz_axis(self, profile_token: str, axis: str, speed: float, duration: float) -> None:
        """Одно движение по одной оси PTZ."""
        velocity = {'PanTilt': {'x': 0.0, 'y': 0.0}}
        velocity['PanTilt'][axis] = speed

        logger.info("PTZ движение по оси %s, скорость=%s, %s сек.", axis, speed, duration)
        self._ptz_service.ContinuousMove(ProfileToken=profile_token, Velocity=velocity)
        time.sleep(duration)
        self._ptz_service.Stop(ProfileToken=profile_token, PanTilt=True, Zoom=True)
        logger.info("PTZ движение по оси %s остановлено.", axis)

    def ptz_move(self, profile_token: str | None = None, x: float = 0.5, y: float = 0.0, duration: float = 1.0):
        """Управление поворотом PTZ.

        x: скорость панорамы, y: скорость наклона (от -1.0 до 1.0).

        Прошивка этой камеры крутит камеру в обратную сторону от знака
        скорости по горизонтали: «Влево» в клавиатуре даёт поворот вправо.
        Поэтому знак оси Pan разворачивается (`ptz_invert_x`, по умолчанию
        включено). Наклон (`ptz_invert_y`) по умолчанию не инвертируется.

        Камера не отрабатывает диагональ в один шаг, поэтому выполняем движение
        поочерёдно: сначала по горизонтали (Pan), затем по вертикали (Tilt).
        """
        if not x and not y:
            return

        if not self._init_ptz():
            logger.warning("Команда PTZ пропущена: ONVIF-сервис недоступен.")
            return

        if self.config.get('ptz_invert_x', self.default_ptz_invert_x):
            x = -x
        if self.config.get('ptz_invert_y', self.default_ptz_invert_y):
            y = -y

        token = profile_token or self._ptz_profile_token or '000'
        try:
            for axis, speed in (('x', x), ('y', y)):
                if speed:
                    self._ptz_axis(token, axis, speed, duration)
        except Exception as error:
            logger.error("Ошибка при выполнении PTZ-команды Xiongmai: %s", error)
            # Сбрасываем клиент, чтобы при следующей попытке переподключиться
            self._ptz_service = None
            self._onvif_client = None

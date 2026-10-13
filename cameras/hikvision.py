# Модуль работы с камерами Hikvision
import io
import logging
import os
import time
from urllib.parse import quote, urlparse, urlunparse

import av
import requests
from onvif import ONVIFClient
from requests.auth import HTTPDigestAuth

from cameras.camera import Camera, CameraOffline

logger = logging.getLogger(__name__)


class Hikvision(Camera):
    # Камера с поворотом (PTZ), одним объективом
    has_ptz: bool = True

    # Таймаут HTTP-запросов к камере, секунды
    request_timeout: float = 5.0
    # Сколько раз пробуем восстановить ONVIF-соединение и пауза между попытками
    connect_attempts: int = 2
    connect_delay: float = 3.0

    def __init__(self, config: dict):
        super().__init__(config)
        self.auth = HTTPDigestAuth(self.config['user'], self.config['pass'])
        self.client = None
        self.media = None
        self.profiles = []
        self.profile = None
        self.image_uri = None
        # Камеры может не быть в сети — на старте не падаем, подключимся позже
        self.connect()

    def connect(self) -> bool:
        """Подключение (или переподключение) к камере по ONVIF."""
        try:
            self.client = ONVIFClient(self.config['host'], self.config['port'],
                                      self.config['user'], self.config['pass'])
            self.media = self.client.media()
            self.profiles = self.media.GetProfiles()
            self.profile = self.profiles[0]  # use the first profile
            self.image_uri = self.media.GetSnapshotUri(ProfileToken=self.profile.token).Uri
            logger.info("Камера %s на связи. Профиль: %s, снимок: %s",
                        self.config['host'], self.profile.Name, self.image_uri)
            return True
        except Exception as error:
            logger.error("Камера %s недоступна: %s", self.config['host'], error)
            self.disconnect()
            return False

    def disconnect(self) -> None:
        """Забываем ONVIF-сессию: следующий запрос переподключится."""
        self.client = None
        self.media = None
        self.profiles = []
        self.profile = None
        self.image_uri = None

    def resolve_profile_token(self, profile_token: str | None) -> str:
        """Токен профиля, который камера действительно знает.

        В конфиге профиль может быть не задан или назван так, как его зовёт
        другая камера (Profile_1, PROFILE_000, ...). Камера на чужой токен
        отвечает NoProfile, поэтому неизвестный токен заменяем на первый
        профиль из GetProfiles — тот самый, с которого берём снимки.
        """
        self.ensure_connected()
        tokens = [profile.token for profile in self.profiles]
        if profile_token in tokens:
            return profile_token

        if profile_token:
            logger.warning("Камера %s не знает профиль %s, берём %s (камера отдаёт: %s)",
                           self.config['host'], profile_token, self.profile.token,
                           ', '.join(tokens))
        return self.profile.token

    def ensure_connected(self) -> None:
        """Гарантирует живое ONVIF-соединение, иначе бросает CameraOffline."""
        if self.client is not None:
            return

        for attempt in range(1, self.connect_attempts + 1):
            logger.info("Подключаемся к камере %s (попытка %s из %s)",
                        self.config['host'], attempt, self.connect_attempts)
            if self.connect():
                return
            if attempt < self.connect_attempts:
                time.sleep(self.connect_delay)

        raise CameraOffline(f"камера {self.config['host']} не отвечает")

    def run(self):
        pass

    def get_image(self, profile_token: str | None = None) -> bytes:
        last_error = ''
        # Две попытки: камера могла перезагрузиться, тогда спасает переподключение
        for attempt in (1, 2):
            self.ensure_connected()
            logger.info(f"Пытаемся скачать скриншот по URL: %s", self.image_uri)
            try:
                # Скачиваем изображение с поддержкой Digest-авторизации hikvision
                response = requests.get(self.image_uri, auth=self.auth, timeout=self.request_timeout)
                response.raise_for_status()
                return response.content
            except requests.RequestException as error:
                last_error = str(error)
                logger.warning("Снимок не получен (попытка %s из 2): %s", attempt, error)
                # Сбрасываем сессию, чтобы следующая попытка началась с подключения
                self.disconnect()

        raise CameraOffline(f"камера {self.config['host']} не отдаёт снимок: {last_error}")

    def get_all_image(self, profile_token: str | None = None) -> bytes:
        """ У этой камеры один объектив """
        return self.get_image(profile_token)

    def _get_stream_uri(self,  profile_token: str) -> str:
        """Возвращает RTSP-ссылку для указанного профиля"""
        uri = self.media.GetStreamUri(
            StreamSetup={'Stream': 'RTP-Unicast', 'Transport': {'Protocol': 'RTSP'}},
            ProfileToken=profile_token,
        )
        return uri.Uri


    def _add_credentials(self, uri: str, user: str, password: str) -> str:
        """Подставляет логин/пароль в RTSP-ссылку: rtsp://user:pass@ip:554/..."""
        parts = urlparse(uri)
        host = parts.hostname or self.config['host']
        netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}"
        if parts.port:
            netloc += f":{parts.port}"
        return urlunparse(parts._replace(netloc=netloc))

    @staticmethod
    def _encode_jpeg(frame: av.VideoFrame) -> bytes:
        """Кодирует один кадр в JPEG средствами PyAV (без внешнего ffmpeg и PIL)."""
        frame = frame.reformat(format='yuvj420p')  # mjpeg-энкодер ждёт full-range YUV
        frame.pts = None
        buf = io.BytesIO()
        with av.open(buf, mode='w', format='mjpeg') as output:
            stream = output.add_stream('mjpeg')
            stream.width = frame.width
            stream.height = frame.height
            stream.pix_fmt = 'yuvj420p'
            for packet in stream.encode(frame):
                output.mux(packet)
            for packet in stream.encode(None):  # дожать буфер энкодера
                output.mux(packet)
        return buf.getvalue()

    def _get_enhancer(self):
        """Улучшатель снимка из config (`enhance`), создаётся один раз. None — выключено."""
        if not hasattr(self, '_enhancer'):
            from cameras.enhance import make_enhancer  # ленивый импорт: cv2 нужен только с enhance
            self._enhancer = make_enhancer(self.config.get('enhance'))
        return self._enhancer

    def _snapshot_from_stream(self, source: str, what: str) -> bytes:
        """Первый кадр RTSP-потока `source` как JPEG. Общий метод для снимков из RTSP.

        what: человекочитаемое имя потока/объектива для сообщений об ошибке.
        Если в config задан `enhance` — прогоняем кадр через улучшатель.
        Если камеры нет в сети — CameraOffline.
        """
        try:
            # timeout=(на открытие, на чтение) — чтобы не зависнуть, если камера пропала
            with av.open(source, options={'rtsp_transport': 'tcp'}, timeout=(10, 10)) as container:
                frame = next(container.decode(video=0))
        except (av.error.FFmpegError, StopIteration) as e:
            logger.error("Не удалось получить кадр (%s): %s", what, e)
            self.disconnect()
            raise CameraOffline(f"камера {self.config['host']} не отдаёт снимок ({what}): {e}")

        enhancer = self._get_enhancer()
        if enhancer is None:
            return self._encode_jpeg(frame)

        # Улучшаем и кодируем средствами OpenCV (кадр уже в памяти)
        import cv2
        bgr = enhancer(frame.to_ndarray(format='bgr24'))
        ok, buf = cv2.imencode('.jpg', bgr, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok:
            logger.warning("Не удалось закодировать улучшенный кадр, отдаём без обработки")
            return self._encode_jpeg(frame)
        return buf.tobytes()


    def get_video(self, profile_token: str | None = None, duration: float = 3.0, filename: str = '') -> str| None:
        """
        Сохраняет видеоролик длительностью `duration` секунд из RTSP-потока камеры.
        Пакеты перекладываются в MP4 как есть (без перекодирования) средствами PyAV.

        profile_token: токен медиа-профиля (например, 'Profile_1')
        duration:      длительность ролика в секундах
        filename:      имя файла
        transport:     транспорт RTSP: 'tcp' (надёжнее) или 'udp'

        Возвращает путь к сохранённому файлу.
        Если камеры нет в сети — бросает CameraOffline.
        """
        self.ensure_connected()
        profile_token = self.resolve_profile_token(profile_token)
        try:
            uri = self._get_stream_uri(profile_token)
        except Exception as e:
            logger.error("Не удалось получить RTSP-ссылку для профиля %s: %s", profile_token, e)
            self.disconnect()
            raise CameraOffline(f"камера {self.config['host']} не отдаёт RTSP-ссылку: {e}")

        logger.info("RTSP URL: %s",uri)
        source = self._add_credentials(uri, self.config['user'], self.config['pass'])
        return self._record_stream(source, duration, filename)

    def _record_stream(self, source: str, duration: float, filename: str) -> str | None:
        """Пишет `duration` секунд RTSP-потока `source` в MP4 `filename` без перекодирования.

        Пакеты перекладываются в контейнер как есть средствами PyAV. Метод общий
        для основного профиля (ONVIF) и произвольной RTSP-ссылки (второй объектив),
        поэтому RTSP-ссылку с логином/паролем готовит вызывающий код.

        Возвращает путь к файлу, None — если не получено ни одного кадра.
        Если поток не открылся или оборвался — бросает CameraOffline.
        """
        logger.info("Запись %s сек. в '%s'...", duration, filename)

        options = {'rtsp_transport': 'tcp'}
        started = time.monotonic()
        first_ts = None
        frames = 0

        try:
            # timeout=(на открытие, на чтение) в секундах — чтобы не зависнуть, если камера пропала
            with av.open(source, options=options, timeout=(10, 10)) as source_container, \
                    av.open(filename, mode='w') as output:

                in_stream = source_container.streams.video[0]
                # Копируем параметры входного потока (кодек, разрешение, time_base)
                out_stream = output.add_stream_from_template(in_stream)

                for packet in source_container.demux(in_stream):
                    if packet.dts is None:  # служебный пакет в конце демукса
                        continue

                    if first_ts is None:
                        # Начинаем с ключевого кадра, иначе начало ролика будет "рассыпаться"
                        if not packet.is_keyframe:
                            continue
                        first_ts = packet.dts

                    # Сдвигаем метки времени к нулю: поток с камеры начинается не с 0
                    packet.dts -= first_ts
                    if packet.pts is not None:
                        packet.pts -= first_ts

                    elapsed = float(packet.dts * in_stream.time_base)

                    packet.stream = out_stream
                    output.mux(packet)
                    frames += 1

                    if elapsed >= duration:
                        break
                    # Страховка на случай некорректных меток времени в потоке
                    if time.monotonic() - started > duration + 30:
                        logger.warning("Превышено время ожидания потока, запись прервана.")
                        break
        except av.error.FFmpegError as e:
            logger.error(f"Ошибка записи потока: {e}")
            # Поток не открылся или оборвался — считаем, что камера пропала
            self.disconnect()
            raise CameraOffline(f"камера {self.config['host']} не отдаёт видеопоток: {e}")

        if not frames or not os.path.exists(filename):
            logger.warning("Не получено ни одного кадра, файл не создан.")
            return None

        size_mb = os.path.getsize(filename) / (1024 * 1024)
        logger.info("Видео успешно сохранено: '%s' (%s кадров, {%s} МБ)", filename, frames, round(size_mb, 2))
        return filename


    def _ptz_axis(self, ptz_service, profile_token: str | None, axis: str, speed: float, duration: float) -> None:
        """
        Одно непрерывное движение по одной оси.
        axis:  'x' — панорама (Pan), 'y' — наклон (Tilt)
        speed: скорость от -1.0 до 1.0
        """
        # Вторая ось строго 0: камера не отрабатывает две оси одновременно
        velocity = {'PanTilt': {'x': 0.0, 'y': 0.0}}
        velocity['PanTilt'][axis] = speed

        logger.info("Запуск движения по оси %s, скорость=%s, %s сек.", axis, speed, duration)
        ptz_service.ContinuousMove(ProfileToken=profile_token, Velocity=velocity)

        # Ждем, пока камера движется
        time.sleep(duration)

        # Обязательно останавливаем движение
        ptz_service.Stop(ProfileToken=profile_token, PanTilt=True, Zoom=True)
        logger.info("Движение по оси %s остановлено.", axis)

    def ptz_move(self, profile_token: str | None = None, x: float = 0.5, y: float = 0.0, duration=1.0):
        """
        Управление поворотом (PTZ) через ContinuousMove.
        x: Скорость панорамирования (Pan), от -1.0 (влево) до 1.0 (вправо)
        y: Скорость наклона (Tilt), от -1.0 (вниз) до 1.0 (вверх)
        duration: Время движения в секундах по каждой оси

        Камера не умеет двигаться сразу по двум осям, поэтому диагональ
        выполняем в два приёма: сначала панорама, затем наклон. Каждый шаг
        длится duration секунд, между шагами движение останавливается,
        то есть диагональ занимает вдвое больше времени, чем прямое движение.
        """
        if not x and not y:
            logger.info("Нулевая скорость по обеим осям, движение не требуется.")
            return

        # Камеры нет в сети — пусть об этом узнает вызывающий код
        self.ensure_connected()
        profile_token = self.resolve_profile_token(profile_token)

        try:
            ptz_service = self.client.ptz()

            for axis, speed in (('x', x), ('y', y)):
                if not speed:
                    continue
                self._ptz_axis(ptz_service, profile_token, axis, speed, duration)

        except Exception as e:
            # Не роняем команду из-за поворота: снимок ещё может получиться,
            # а сессию сбрасываем, чтобы следующий запрос переподключился
            logger.error(f"Ошибка PTZ-управления: {e}")
            self.disconnect()

# Модуль работы с камерами Tenda
#
# Проверено на Tenda CH9 (прошивка V23.5.11.64) по ONVIF/RTSP.
# Камера почти во всём совместима с Hikvision, поэтому берём его класс за основу
# и переопределяем только то, что реально отличается:
#
#   1. ONVIF-снимок (getsnapshot) отдаёт лишь 640×368 независимо от профиля,
#      тогда как основной RTSP-поток — 2304×1296. Поэтому get_image берём не
#      из HTTP-снимка, а первым кадром основного RTSP-потока (полное разрешение).
#   2. PTZ, как и у Hikvision, не умеет двигаться сразу по двум осям
#      (диагональ -> SOAP "Action Not Implemented"), поэтому логика разбиения
#      диагонали на два движения из Hikvision подходит без изменений.
#   3. Камера "сдвоенная": поворотный объектив (RTSP ch=1) и статический
#      (RTSP ch=2). В ONVIF выведен только ch=1 — два профиля (main/sub).
#      Второй объектив доступен лишь по "сырой" RTSP-ссылке, без ONVIF-снимка,
#      поэтому для него добавлены get_second_image / get_second_video.
#
# Клавиатуры и раскладку PTZ переиспользуем из hikvision — они не зависят
# от модели камеры. Реэкспортируем их здесь, чтобы воркеры могли писать
# `from cameras.tenda import Tenda, make_keyboard, ...` по аналогии с Hikvision.
import logging
import re

from cameras.camera import CameraOffline
from cameras.hikvision import Hikvision
from cameras.menu import (
    keyboard_layout,
    make_keyboard,
    make_telegram_keyboard,
    ptz_directions,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Tenda",
    "keyboard_layout",
    "make_keyboard",
    "make_telegram_keyboard",
    "ptz_directions",
]


class Tenda(Hikvision):
    """Камера Tenda (проверено на CH9).

    Отличия от Hikvision:
      * снимок берётся из RTSP-потока (ONVIF-снимок Tenda всего 640×368);
      * два объектива: get_image/get_video/ptz_move работают с поворотным
        объективом (RTSP ch=1), а get_second_image/get_second_video —
        со статическим (RTSP ch=2), у которого нет ни ONVIF-профиля,
        ни ONVIF-снимка.

    Всё остальное — подключение по ONVIF, профили, RTSP, PTZ — наследуется.
    """

    # Сдвоенная камера: поворотный объектив (has_ptz наследуется от Hikvision)
    # плюс статический второй (get_second_image / get_second_video).
    has_second_lens: bool = True

    def get_image(self, profile_token: str | None = None) -> bytes:
        """Снимок основного (поворотного) объектива в полном разрешении.

        ONVIF-снимок Tenda (getsnapshot) отдаёт лишь 640×368 независимо от
        профиля, поэтому берём первый кадр из основного RTSP-потока и кодируем
        его в JPEG. Если камеры нет в сети — CameraOffline.
        """
        self.ensure_connected()
        profile_token = self.resolve_profile_token(profile_token)
        try:
            uri = self._get_stream_uri(profile_token)
        except Exception as e:
            logger.error("Не удалось получить RTSP-ссылку для профиля %s: %s", profile_token, e)
            self.disconnect()
            raise CameraOffline(f"камера {self.config['host']} не отдаёт RTSP-ссылку: {e}")

        source = self._add_credentials(uri, self.config['user'], self.config['pass'])
        return self._snapshot_from_stream(source, 'основной объектив')

    def _second_stream_uri(self, subtype: int = 0) -> str:
        """RTSP-ссылка второго (статического) объектива с логином/паролем.

        ONVIF отдаёт ссылку только на первый объектив (ch=1). Берём её и
        подменяем номер канала на второй — так host/port/путь остаются такими,
        какими их знает камера, а меняется только объектив. Номер второго
        канала при необходимости переопределяется в конфиге (`second_channel`).

        subtype: 0 — основной поток, 1 — субпоток.
        """
        self.ensure_connected()
        try:
            base = self._get_stream_uri(self.profile.token)
        except Exception as e:
            logger.error("Не удалось получить RTSP-ссылку камеры %s: %s", self.config['host'], e)
            self.disconnect()
            raise CameraOffline(f"камера {self.config['host']} не отдаёт RTSP-ссылку: {e}")

        channel = self.config.get('second_channel', 2)
        uri, n = re.subn(r'ch=\d+', f'ch={channel}', base)
        if not n:
            logger.warning("В RTSP-ссылке %s нет 'ch=', второй объектив может быть недоступен", base)
        uri = re.sub(r'subtype=\d+', f'subtype={subtype}', uri)

        logger.info("RTSP URL второго объектива: %s", uri)
        return self._add_credentials(uri, self.config['user'], self.config['pass'])

    def get_second_image(self, subtype: int = 0) -> bytes:
        """Снимок со статического объектива (RTSP ch=2) как JPEG.

        У этого объектива нет ONVIF-снимка, поэтому берём первый кадр из
        RTSP-потока и кодируем его в JPEG. Если камеры нет в сети — CameraOffline.
        """
        return self._snapshot_from_stream(self._second_stream_uri(subtype), 'второй объектив')

    def get_all_image(self, profile_token: str | None = None) -> bytes:
        """Снимок обоих объективов одним изображением (JPEG).

        Берём снимок первого (поворотного) и второго (статического) объективов
        и склеиваем вертикально: первый сверху, второй снизу. Если у кадров
        разная ширина, нижний приводится к ширине верхнего. Если камеры нет
        в сети — CameraOffline.
        """
        first = self.get_image(profile_token)
        second = self.get_second_image()
        return self._stack_jpegs(first, second)

    @staticmethod
    def _stack_jpegs(top: bytes, bottom: bytes) -> bytes:
        """Склеивает два JPEG вертикально средствами OpenCV (без PIL)."""
        import cv2
        import numpy as np

        first = cv2.imdecode(np.frombuffer(top, np.uint8), cv2.IMREAD_COLOR)
        second = cv2.imdecode(np.frombuffer(bottom, np.uint8), cv2.IMREAD_COLOR)
        if first is None or second is None:
            raise RuntimeError('не удалось раскодировать снимок для склейки')

        height, width = first.shape[:2]
        if second.shape[1] != width:
            # Приводим нижний кадр к ширине верхнего, сохраняя пропорции
            scale = width / second.shape[1]
            second = cv2.resize(second, (width, round(second.shape[0] * scale)),
                                interpolation=cv2.INTER_AREA)

        ok, buf = cv2.imencode('.jpg', np.vstack((first, second)),
                               [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok:
            raise RuntimeError('не удалось закодировать склеенный снимок')
        return buf.tobytes()

    def get_second_video(self, duration: float = 3.0, filename: str = '', subtype: int = 0) -> str | None:
        """Видеоролик со статического объектива (RTSP ch=2).

        Пишет `duration` секунд в MP4 `filename` без перекодирования тем же
        методом, что и основной get_video. Возвращает путь к файлу или None,
        если не получено ни одного кадра.

        subtype: 0 — основной поток, 1 — субпоток.
        """
        source = self._second_stream_uri(subtype)
        return self._record_stream(source, duration, filename)

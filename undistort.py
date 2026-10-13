"""
Коррекция бочкообразной дисторсии Hikvision DS-2DE2C400MWG-E.

Коэффициенты получены plumb-line методом по линии горизонта на data/hikvision.jpg
(остаточная невязка 2.4 px на 999 точках). Модель Брауна-Конради, только радиальная часть.

k1/k2 заданы в НОРМАЛИЗОВАННЫХ координатах и не зависят от разрешения,
а fx/fy/cx/cy — зависят, поэтому K строится от ширины кадра.
"""
import cv2
import numpy as np

F_SCALE = 0.75          # fx = fy = 0.75 * width  (~67 deg HFOV)
K1, K2 = -0.4301, 0.1366
ROLL_DEG = 2.6          # физический завал камеры, найденный тем же фитом


def build_maps(width, height, alpha=0.0, fix_roll=False):
    """alpha=0 — обрезать до валидной области; alpha=1 — сохранить все пиксели (с чёрными полями)."""
    f = F_SCALE * width
    K = np.array([[f, 0, width / 2], [0, f, height / 2], [0, 0, 1]], np.float64)
    D = np.array([K1, K2, 0.0, 0.0], np.float64)
    newK, _ = cv2.getOptimalNewCameraMatrix(K, D, (width, height), alpha, (width, height))
    R = None
    if fix_roll:
        a = np.deg2rad(ROLL_DEG)
        R = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]], np.float64)
    return cv2.initUndistortRectifyMap(K, D, R, newK, (width, height), cv2.CV_16SC2)


class Undistorter:
    """Карты строятся один раз, дальше remap — ~1-2 мс на 1080p."""

    def __init__(self, alpha=0.0, fix_roll=False):
        self.alpha, self.fix_roll = alpha, fix_roll
        self._size = None
        self._maps = None

    def __call__(self, frame):
        h, w = frame.shape[:2]
        if self._size != (w, h):
            self._size = (w, h)
            self._maps = build_maps(w, h, self.alpha, self.fix_roll)
        return cv2.remap(frame, *self._maps, cv2.INTER_LINEAR)


if __name__ == "__main__":
    import sys
    src, dst = sys.argv[1], sys.argv[2]
    img = cv2.imread(src)
    cv2.imwrite(dst, Undistorter(alpha=0.0)(img), [cv2.IMWRITE_JPEG_QUALITY, 92])
    print("saved", dst)

"""
Классическое улучшение снимка под общую резкость — без нейросетей и без
«выдуманных» деталей (важно для охранной камеры: ничего не дорисовываем).

Порядок обработки BGR-кадра:
  1. bilateral — убирает шум, сохраняя края (в отличие от размытия);
  2. CLAHE по каналу яркости L — локальный контраст, вытягивает тени/детали;
  3. unsharp mask — повышает резкость.

Работает над ndarray (BGR), как Undistorter из undistort.py. Тяжёлый
NLM-денойз намеренно не используем: на кадре 2304×1296 он ~8 с, а bilateral
даёт похожий результат за ~0.2 с.
"""
import cv2


class Enhancer:
    """Улучшатель кадра. Параметры настраиваются, объект переиспользуется.

    denoise:        сила bilateral-денойза (sigma), 0 — выключить;
    clahe_clip:     сила локального контраста CLAHE, 0 — выключить;
    sharpen:        сила резкости unsharp mask, 0 — выключить;
    sharpen_radius: радиус размытия для unsharp mask, пиксели.
    """

    def __init__(self, denoise: float = 50.0, clahe_clip: float = 2.0,
                 sharpen: float = 0.6, sharpen_radius: float = 1.2):
        self.denoise = denoise
        self.sharpen = sharpen
        self.sharpen_radius = sharpen_radius
        # Объект CLAHE создаём один раз (clipLimit=0 — фактически выключен)
        self._clahe = cv2.createCLAHE(clipLimit=clahe_clip, tileGridSize=(8, 8)) if clahe_clip else None

    def __call__(self, bgr):
        if self.denoise:
            bgr = cv2.bilateralFilter(bgr, 5, self.denoise, self.denoise)

        if self._clahe is not None:
            lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
            l, a, b = cv2.split(lab)
            l = self._clahe.apply(l)
            bgr = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)

        if self.sharpen:
            blur = cv2.GaussianBlur(bgr, (0, 0), self.sharpen_radius)
            bgr = cv2.addWeighted(bgr, 1 + self.sharpen, blur, -self.sharpen, 0)

        return bgr


def make_enhancer(setting) -> Enhancer | None:
    """Строит Enhancer по значению `enhance` из config.yml.

    setting может быть:
      * ложным (None/False/отсутствует) -> None, улучшение выключено;
      * True -> Enhancer с параметрами по умолчанию;
      * dict -> Enhancer с переопределёнными параметрами, например:
            enhance:
              denoise: 40
              clahe_clip: 2.5
              sharpen: 0.7
    """
    if not setting:
        return None
    if setting is True:
        return Enhancer()
    if isinstance(setting, dict):
        return Enhancer(**setting)
    raise ValueError(f"Некорректное значение enhance: {setting!r} (ожидается true или словарь параметров)")

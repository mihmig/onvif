# Клавиатура управления камерой для мессенджеров.
#
# Набор кнопок собирается по возможностям камеры (camera.has_ptz,
# camera.has_second_lens), а не задаётся под каждую камеру вручную:
#
#   * есть PTZ  -> матрица стрелок 3×3 с «Фото» в центре и кнопкой «Видео» внизу;
#   * нет PTZ   -> две кнопки: «Фото» и «Видео»;
#   * сдвоенная -> добавляется ряд «Фото [2]» и «Видео [2]» для второго объектива.
#
# Раскладка одна на все мессенджеры; конкретный формат кнопок собирают
# make_keyboard (MAX) и make_telegram_keyboard (Telegram).
from typing import Dict, List, Tuple

# Направление движения камеры для callback-команд клавиатуры:
# payload -> (по горизонтали, по вертикали) в долях от скорости ptz_speed.
ptz_directions: Dict[str, Tuple[float, float]] = {
    "ptz-left-up": (-1.0, 1.0),
    "ptz-up": (0.0, 1.0),
    "ptz-right-up": (1.0, 1.0),
    "ptz-left": (-1.0, 0.0),
    "ptz-right": (1.0, 0.0),
    "ptz-left-down": (-1.0, -1.0),
    "ptz-down": (0.0, -1.0),
    "ptz-right-down": (1.0, -1.0),
}


def keyboard_layout(video_duration: float = 10.0,
                    has_ptz: bool = True,
                    has_second_lens: bool = False) -> List[List[Tuple[str, str]]]:
    """Раскладка кнопок управления: строки из пар (подпись, команда).

    video_duration:  длительность ролика, подписываем на кнопке «Видео».
    has_ptz:         добавить матрицу стрелок 3×3 (иначе — только Фото/Видео).
    has_second_lens: добавить ряд кнопок «[2]» для второго объектива.
    """
    video_label = f"🎥 Видео ({video_duration:g} с.)"
    rows: List[List[Tuple[str, str]]] = []

    if has_second_lens: # Камера со статическим и поворотным объективом
        rows += [
            [("↖️", "ptz-left-up"), ("⬆️", "ptz-up"), ("↗️", "ptz-right-up")],
            [("⬅️", "ptz-left"),
             ("📸[1]", "make-foto"), ("📸[2]", "make-foto-2"), ("📸[1+2]", "make-foto-all"),
             ("➡️", "ptz-right")],
            [("↙️", "ptz-left-down"), ("⬇️", "ptz-down"), ("↘️", "ptz-right-down")],
            [(video_label, "make-video"), (f"{video_label} [2]", "make-video-2")],
        ]
    elif has_ptz: # Камера с поворотным объективом
        rows += [
            [("↖️", "ptz-left-up"), ("⬆️", "ptz-up"), ("↗️", "ptz-right-up")],
            [("⬅️", "ptz-left"), ("📸 Фото", "make-foto"), ("➡️", "ptz-right")],
            [("↙️", "ptz-left-down"), ("⬇️", "ptz-down"), ("↘️", "ptz-right-down")],
            [(video_label, "make-video")],
        ]
    else: # Обычная камера
        rows.append([("📸 Фото", "make-foto"), (video_label, "make-video")])
    return rows


def make_keyboard(video_duration: float = 10.0,
                  has_ptz: bool = True,
                  has_second_lens: bool = False) -> Dict:
    """Клавиатура управления камерой для MAX."""
    return {
        "type": "inline_keyboard",
        "payload": {
            "buttons": [
                [{"type": "callback", "text": text, "payload": payload} for text, payload in row]
                for row in keyboard_layout(video_duration, has_ptz, has_second_lens)
            ]
        }
    }


def make_telegram_keyboard(video_duration: float = 10.0,
                           has_ptz: bool = True,
                           has_second_lens: bool = False) -> Dict:
    """Та же клавиатура в формате Telegram: команда кнопки лежит в callback_data.

    Формат: https://core.telegram.org/bots/api#inlinekeyboardmarkup
    """
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": payload} for text, payload in row]
            for row in keyboard_layout(video_duration, has_ptz, has_second_lens)
        ]
    }

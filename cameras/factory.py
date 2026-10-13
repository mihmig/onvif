# Фабрика камер: по полю `type` из config.yml создаёт нужный класс.
#
# Так воркеры не привязаны к конкретной модели — камеру выбирает конфиг.
from typing import List

from cameras.camera import Camera
from cameras.hikvision import Hikvision
from cameras.tenda import Tenda
from cameras.xiongmai import Xiongmai

# Значение поля `type` в config.yml -> класс камеры
CAMERA_TYPES: dict[str, type[Camera]] = {
    "hikvision": Hikvision,
    "tenda": Tenda,
    "xiongmai": Xiongmai,
}


def create_camera(config: dict) -> Camera:
    """Создаёт камеру по `type` из конфига (по умолчанию — hikvision)."""
    camera_type = (config.get('type') or 'hikvision').lower()
    try:
        camera_class = CAMERA_TYPES[camera_type]
    except KeyError:
        raise ValueError(
            f"Неизвестный тип камеры '{camera_type}'. "
            f"Допустимые: {', '.join(sorted(CAMERA_TYPES))}"
        )
    return camera_class(config)

def find_camera_config(cameras: List[dict], camera_id: str) -> dict:
    """Секция камеры из config.yml по её id.

    Камеру задаёт первый аргумент запуска, например:
        python worker_telegram.py Tenda-CH9-telegram
    Нет аргумента или id не найден — выходим с подсказкой по доступным id.
    """
    # В config.yml лежат камеры и для MAX, и для Telegram — оставляем только телеграмные
    # cameras = [item for item in cameras if item.get('telegram_bot_token')]
    available = ', '.join(item.get('id', '?') for item in cameras)
    if not camera_id:
        raise SystemExit(
            f'Укажите id камеры из config.yml, например: python worker_telegram.py Tenda-CH9-telegram\n'
            f'или python worker_max.py Tenda-CH9-max\n'
            f'Доступные id: {available}'
        )
    for item in cameras:
        if item.get('id') == camera_id:
            return item
    raise SystemExit(f'Камера с id "{camera_id}" не найдена в config.yml. Доступные id: {available}')
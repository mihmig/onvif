from abc import ABCMeta, abstractmethod, ABC


class CameraOffline(Exception):
    """ Камера не отвечает: нет сети или питания, ONVIF-сессия потеряна """


class Camera(ABC):
    """ Базовый класс камеры """
    __metaclass__ = ABCMeta
    __slots__ = ('config', 'name')

    # Возможности камеры (переопределяются в наследниках). По ним воркер
    # собирает клавиатуру управления: есть ли поворот и второй объектив.
    has_ptz: bool = False
    has_second_lens: bool = False

    def __init__(self, config: dict) -> None:
        self.config = config
        self.name = config.get('name') or 'Not set'

    @abstractmethod
    def run(self):
        pass

    @abstractmethod
    def get_image(self, profile_token: str | None) -> bytes:
        pass

    @abstractmethod
    def get_all_image(self, profile_token: str | None = None) -> bytes:
        pass

    @abstractmethod
    def get_video(self, profile_token: str | None = None, duration: float = 3.0, filename: str = '') -> str|None:
        pass

    @abstractmethod
    def ptz_move(self, profile_token: str | None = None, x: float = 0.5, y:  float = 0.0, duration=1.0):
        pass


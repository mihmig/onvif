import logging
from abc import ABCMeta, abstractmethod, ABC
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class Sender(ABC):
    """ Базовый класс отправки сообщений """
    __metaclass__ = ABCMeta
    __slots__ = ('config', 'name')

    def __init__(self, config: dict) -> None:
        self.config = config
        self.name = config.get('name') or 'Not set'

    @abstractmethod
    def send_message(self, to: str, message: str):
        pass

    @abstractmethod
    def send_image(self, to: str, caption: str, photo: bytes):
        pass


# Отправитель и чат по конфигу камеры: берётся первый мессенджер, у которого
# задан токен и идентификатор чата. Порядок списка — порядок предпочтения,
# если настроены оба. Чаты Telegram и MAX разные, поэтому у каждого свой
# ключ, а общий chat_id служит запасным вариантом.
def create_sender(config: dict) -> Tuple[Optional[Sender], Optional[int]]:
    # Импорты внутри функции: telegram_sender и max_sender наследуют этот
    # модуль, на уровне модуля получился бы цикл импортов.
    from plugins.max_sender import MaxSender
    from plugins.telegram_sender import TelegramSender

    messengers = [
        (TelegramSender, 'telegram_bot_token', ('telegram_chat_id', 'chat_id')),
        (MaxSender, 'max_bot_token', ('max_chat_id', 'chat_id')),
    ]
    for cls, token_key, chat_keys in messengers:
        if not config.get(token_key):
            continue
        chat_id = next((config[key] for key in chat_keys if config.get(key)), None)
        if chat_id is None:
            logger.warning('%s: токен есть, а чат (%s) не задан — пропускаем',
                           cls.__name__, ' или '.join(chat_keys))
            continue
        return cls(config), chat_id

    logger.info('Мессенджер не настроен: %s не заданы',
                ' / '.join(key for _, key, _ in messengers))
    return None, None

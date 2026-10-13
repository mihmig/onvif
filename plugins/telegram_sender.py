# Плагин работы с ботом Telegram: отправка сообщений и медиа,
# редактирование, удаление и приём апдейтов методом long polling.
# Описание Bot API: https://core.telegram.org/bots/api
#
# Отличия от MaxSender (plugins/max_sender.py):
# - сообщение адресуется парой (chat_id, message_id), одного идентификатора мало;
# - отдельной загрузки файла нет: медиа уходит тем же запросом, что и сообщение,
#   поэтому метода upload_media здесь нет;
# - текстовое сообщение нельзя превратить в медиа: для текста работает
#   editMessageText, для медиа — editMessageCaption, поменять тип нельзя;
# - на каждое нажатие кнопки нужно ответить answerCallbackQuery,
#   иначе у пользователя надолго повисают «часики» на кнопке.
import json
import logging
import mimetypes
import os
import time
from typing import Any, Dict, List

import requests
from pydantic import ValidationError

from plugins.sender import Sender
from plugins.telegram_models import Message, MessageResult, Update

logger = logging.getLogger(__name__)


class TelegramSender(Sender):
    # Метод API, имя поля с файлом, имя файла и Content-Type по умолчанию
    # для каждого типа медиа. Ключ image оставлен для совместимости с MAX.
    media_defaults: Dict[str, tuple] = {
        "image": ("sendPhoto", "photo", "image.png", "image/png"),
        "photo": ("sendPhoto", "photo", "image.png", "image/png"),
        "video": ("sendVideo", "video", "video.mp4", "video/mp4"),
        "audio": ("sendAudio", "audio", "audio.mp3", "audio/mpeg"),
        "file": ("sendDocument", "document", "file.bin", "application/octet-stream"),
        "document": ("sendDocument", "document", "file.bin", "application/octet-stream"),
    }
    # Таймаут обычного запроса и запроса с загрузкой файла, секунды
    request_timeout: float = 30.0
    upload_timeout: float = 180.0
    # Пауза между повторами сетевого запроса: растёт от network_retry_delay
    # до network_retry_delay_max секунд, пока Telegram не станет доступен.
    network_retry_delay: float = 1.0
    network_retry_delay_max: float = 30.0

    def __init__(self, config: dict):
        super().__init__(config)
        self.api_url = f"https://api.telegram.org/bot{config['telegram_bot_token']}"
        # Идентификатор следующего ожидаемого апдейта (обновляется в get_updates)
        self.offset: int | None = None

    # Запрос к Bot API. Возвращает содержимое поля result из ответа.
    # body   — параметры метода; поля со значением None не отправляются,
    #          поэтому отсутствующий reply_markup означает «клавиатуры нет».
    # files  — файлы для multipart-запроса (загрузка медиа).
    #
    # Сетевые сбои (обрыв связи, таймаут, сброс соединения) переживаем здесь:
    # запрос повторяется неограниченно долго с растущей паузой, пока сеть
    # не восстановится — бот без Telegram всё равно бесполезен, а падать
    # из-за временной недоступности серверов нельзя. Ошибки самого API
    # (не тот chat_id, битый медиафайл и т.п.) не повторяем: ответ не изменится.
    def _call(self,
              method: str,
              body: Dict | None = None,
              files: Dict | None = None,
              timeout: float | None = None) -> Any:
        url = f"{self.api_url}/{method}"
        params = {key: value for key, value in (body or {}).items() if value is not None}

        if files:
            # В multipart вложенные объекты (reply_markup) передаются JSON-строкой
            data = {key: (json.dumps(value, ensure_ascii=False)
                          if isinstance(value, (dict, list)) else value)
                    for key, value in params.items()}
            kwargs: Dict = dict(data=data, files=files,
                                timeout=timeout or self.upload_timeout)
        else:
            kwargs = dict(json=params, timeout=timeout or self.request_timeout)

        delay = self.network_retry_delay
        while True:
            try:
                response = requests.post(url, **kwargs)
                break
            except requests.exceptions.RequestException as error:
                logger.error(f'Telegram: сеть недоступна ({method}): {error}; '
                             f'повторяем через {delay:g} с.')
                time.sleep(delay)
                delay = min(delay * 2, self.network_retry_delay_max)

        try:
            data = response.json()
        except ValueError as error:
            raise IOError(f'{method} response is not JSON: {error} '
                          f'({response.status_code} {response.text})')

        if response.status_code != 200 or not data.get('ok'):
            raise IOError(f'{method} failed: {response.status_code} '
                          f'{data.get("description") or response.text}')

        return data.get('result')

    # Разбор ответа, в котором Telegram возвращает сообщение
    @staticmethod
    def _to_message(result: Any, method: str) -> Message:
        try:
            return MessageResult.model_validate({"ok": True, "result": result}).result
        except ValidationError as error:
            raise IOError(f'Не удалось разобрать ответ {method} {result!r}: {error}')

    # Отправка сообщения в Телеграм-чат.
    # Возвращает отправленное сообщение: в нём message_id, нужный для
    # последующего редактирования/удаления.
    def send_message(self,
                     chat_id: int,
                     text: str,
                     message_format: str | None = None,
                     keyboard: Dict | None = None) -> Message:
        logger.info(f'Telegram: Отправляем сообщение: chat_id: {chat_id}, text: {text}')
        result = self._call('sendMessage', {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": message_format,
            "reply_markup": keyboard,
        })
        message = self._to_message(result, 'sendMessage')
        logger.info(f'Telegram: сообщение отправлено, chat_id: {chat_id}, '
                    f'message_id: {message.message_id}')
        return message

    # Отправка сообщения с медиафайлом (картинка, видео, аудио, файл)
    # и, если передана, клавиатурой управления.
    # media      — байты или путь к файлу.
    # media_type — image/photo | video | audio | file/document.
    # Отдельного шага загрузки, как в MAX, у Telegram нет: файл уходит
    # тем же multipart-запросом, что и сообщение.
    def send_media(self,
                   chat_id: int,
                   caption: str,
                   media: bytes | str,
                   media_type: str = 'image',
                   keyboard: Dict | None = None) -> Message:
        method, field, default_name, default_content_type = \
            self.media_defaults.get(media_type, self.media_defaults['file'])

        # Подготовка данных и имени файла
        if isinstance(media, str):
            with open(media, 'rb') as f:
                media_data = f.read()
            filename = os.path.basename(media)
        else:
            media_data = media
            filename = default_name
        content_type = mimetypes.guess_type(filename)[0] or default_content_type
        logger.info(f'Telegram: Отправляем {media_type}: {filename}, {len(media_data)} байт')

        body: Dict = {
            "chat_id": chat_id,
            "caption": caption,
            "reply_markup": keyboard,
        }
        if field == 'video':
            # Чтобы ролик можно было смотреть, не скачивая целиком
            body["supports_streaming"] = True

        result = self._call(method, body, files={field: (filename, media_data, content_type)})
        message = self._to_message(result, method)
        logger.info(f'Telegram: {media_type} отправлен, chat_id: {chat_id}, '
                    f'message_id: {message.message_id}')
        return message

    # Отправка картинки — частный случай send_media (метод базового класса Sender)
    def send_image(self,
                   chat_id: int,
                   caption: str,
                   photo: bytes | str,
                   keyboard: Dict | None = None) -> Message:
        return self.send_media(chat_id, caption, photo, 'image', keyboard)

    # Редактирование ранее отправленного сообщения.
    # text      — новый текст (для медиа — подпись); None — правим только клавиатуру.
    # keyboard  — новая клавиатура; None — клавиатура убирается.
    # has_media — в сообщении есть медиа: тогда правится подпись, а не текст.
    #             Превратить текстовое сообщение в медиа Telegram не позволяет.
    def edit_message(self,
                     chat_id: int,
                     message_id: int,
                     text: str | None = None,
                     keyboard: Dict | None = None,
                     has_media: bool = False,
                     message_format: str | None = None) -> None:
        logger.info(f'Telegram: Редактируем сообщение: message_id: {message_id}, text: {text}')
        body: Dict = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": keyboard,
        }
        if text is None:
            method = 'editMessageReplyMarkup'
        elif has_media:
            method = 'editMessageCaption'
            body["caption"] = text
            body["parse_mode"] = message_format
        else:
            method = 'editMessageText'
            body["text"] = text
            body["parse_mode"] = message_format

        self._call(method, body)
        logger.info(f'Telegram: сообщение отредактировано, message_id: {message_id}')

    # Удаление сообщения. Бот может удалять свои сообщения не старше 48 часов.
    def delete_message(self, chat_id: int, message_id: int) -> None:
        logger.info(f'Telegram: Удаляем сообщение: message_id: {message_id}')
        self._call('deleteMessage', {"chat_id": chat_id, "message_id": message_id})

    # Ответ на нажатие кнопки: убирает «часики» у пользователя.
    # Ответить нужно в течение нескольких секунд после получения апдейта.
    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        try:
            self._call('answerCallbackQuery',
                       {"callback_query_id": callback_query_id, "text": text})
        except IOError as error:
            # Неотвеченный callback — не повод прерывать обработку команды
            logger.error(f'Telegram: не удалось ответить на callback: {error}')

    # Получение апдейтов методом long polling.
    # offset          — идентификатор первого нужного апдейта; None — взять
    #                   сохранённый self.offset, а при первом запросе — все
    #                   апдейты, которые бот ещё не подтвердил.
    # timeout         — сколько секунд сервер держит соединение, ожидая события.
    # limit           — максимум апдейтов в одном ответе (1..100).
    # allowed_updates — фильтр по типам: message, callback_query, ...
    # Смещение для следующего запроса сохраняется в self.offset: получив его,
    # сервер считает предыдущие апдейты подтверждёнными и больше их не отдаёт.
    def get_updates(self,
                    offset: int | None = None,
                    timeout: int = 30,
                    limit: int = 100,
                    allowed_updates: List[str] | None = None) -> List[Update]:
        if offset is None:
            offset = self.offset

        # Ответ ждём дольше, чем длится long polling на стороне сервера.
        result = self._call('getUpdates', {
            "offset": offset,
            "timeout": timeout,
            "limit": limit,
            "allowed_updates": allowed_updates,
        }, timeout=timeout + 10)

        # Один битый апдейт не должен ронять весь опрос: логируем и пропускаем его,
        # но смещение всё равно сдвигаем, иначе он будет приходить снова и снова.
        updates: List[Update] = []
        for item in result or []:
            update_id = item.get('update_id') if isinstance(item, dict) else None
            if update_id is not None:
                self.offset = max(self.offset or 0, update_id + 1)
            try:
                updates.append(Update.model_validate(item))
            except ValidationError as error:
                logger.error(f'Telegram: не удалось разобрать апдейт {item!r}: {error}')

        if updates:
            logger.info(f'Telegram: получено апдейтов: {len(updates)}, '
                        f'следующее смещение: {self.offset}')
        return updates

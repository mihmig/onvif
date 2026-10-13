# Плагин отправки уведомлений в телеграм.
# Проверка сертификатов:
# - platform-api2.max.ru (сообщения, получение upload url) — по корневому CA
#   Минцифры max_root_ca.crt. Листовые сертификаты платформы меняются,
#   корневой CA стабилен, поэтому достаточно одного корневого сертификата.
# - iu.oneme.ru (загрузка файлов) — публичный корень ISRG Root X1,
#   проверяется стандартным хранилищем requests (verify по умолчанию).
# Обновить root CA при необходимости: py -3 fetch_cert.py
# Официальный источник: https://www.gosuslugi.ru/crt
import logging
import mimetypes
import os
import time
from threading import Thread
from typing import List, Dict

import requests
from pydantic import ValidationError
from retrying import retry

from plugins.max_models import Message, SendMessageResult, Update
from plugins.sender import Sender

logger = logging.getLogger(__name__)

def retry_if_io_error(exception):
    print(f"error: {exception}")
    return isinstance(exception, IOError)


class MaxSender(Sender):
    # Имя файла и Content-Type по умолчанию для каждого типа вложения MAX
    media_defaults: Dict[str, tuple] = {
        "image": ("image.png", "image/png"),
        "video": ("video.mp4", "video/mp4"),
        "audio": ("audio.mp3", "audio/mpeg"),
        "file": ("file.bin", "application/octet-stream"),
    }
    # Сколько раз и с какой паузой ждать, пока MAX обработает вложение
    attachment_attempts: int = 15
    attachment_delay: float = 2.0

    def __init__(self, config: dict):
        super().__init__(config)
        self.api_url = "https://platform-api2.max.ru"
        self.send_message_url = f"{self.api_url}/messages"
        self.upload_url = f"{self.api_url}/uploads"
        self.updates_url = f"{self.api_url}/updates"
        # Маркер последнего полученного апдейта (обновляется в get_updates)
        self.marker: int | None = None
        self.headers = {
            "Authorization": config['max_bot_token'],
            "Content-Type": "application/json"
        }


    # Отправка сообщения в MAX-чат.
    # Возвращает отправленное сообщение: в нём mid и seq, нужные для
    # последующего редактирования/удаления.
    @retry(retry_on_exception=retry_if_io_error,
           wait_exponential_multiplier=1000,
           wait_exponential_max=10000,
           stop_max_attempt_number=3,
           stop_max_delay=60000
           )
    def send_message(self, chat_id: int, text: str, message_format: str = 'html') -> Message:
        logger.info(f'MAX: Отправляем сообщение: chat_id: {chat_id}, text: {text}')
        params = {
            "chat_id": chat_id
        }
        response = requests.post(self.send_message_url,
                                 params=params,
                                 headers=self.headers,
                                 json={"text": text, "format": message_format},
                                 verify="max_root_ca.crt",
                                 )

        if response.status_code != 200:
            raise IOError(f'Send message failed: {response.status_code} {response.text}')

        try:
            data = response.json()
        except ValueError as error:
            raise IOError(f'Send message response is not JSON: {error}')

        try:
            result = SendMessageResult.model_validate(data)
        except ValidationError as error:
            raise IOError(f'Не удалось разобрать ответ на отправку сообщения {data!r}: {error}')

        logger.info(f'MAX: сообщение отправлено, chat_id: {chat_id}, mid: {result.message.body.mid}')
        return result.message

    # Отправка сообщения в отдельном потоке, чтобы не задерживать основной
    def send_message_in_thread(self, chat_id: int, text: str) -> None:
        Thread(target=self.send_message_with_retry, args=(chat_id, text)).start()


    # Загрузка медиафайла в MAX.
    # media      — байты или путь к файлу.
    # media_type — тип вложения MAX: image | video | audio | file.
    # Шаг 1: получение URL для загрузки (uploads?type=...). Для video/audio/file
    #        токен вложения приходит уже здесь, для image — только после загрузки.
    # Шаг 2: загрузка файла multipart-запросом.
    # Возвращает готовое вложение: {"type": ..., "payload": {"token": ...}}.
    @retry(retry_on_exception=retry_if_io_error,
           wait_exponential_multiplier=1000,
           wait_exponential_max=10000,
           stop_max_attempt_number=3,
           stop_max_delay=60000
           )
    def upload_media(self, media: bytes | str, media_type: str = 'image') -> Dict:
        default_name, default_content_type = self.media_defaults.get(media_type,
                                                                    self.media_defaults['file'])
        # Подготовка данных и имени файла
        if isinstance(media, str):
            with open(media, 'rb') as f:
                media_data = f.read()
            filename = os.path.basename(media)
        else:
            media_data = media
            filename = default_name
        content_type = mimetypes.guess_type(filename)[0] or default_content_type
        logger.info(f'MAX: Загружаем {media_type}: {filename}, {len(media_data)} байт')

        # Шаг 1: получение URL для загрузки
        response = requests.post(self.upload_url,
                                 params={"type": media_type},
                                 headers=self.headers,
                                 verify="max_root_ca.crt",
                                 )
        if response.status_code != 200:
            raise IOError(f'Upload url request failed: {response.status_code} {response.text}')
        upload = response.json()
        upload_url = upload['url']
        # Для video/audio/file токен известен уже на этом шаге
        token = upload.get('token')

        # Шаг 2: загрузка файла. iu.oneme.ru использует публичный корень ISRG Root X1,
        # поэтому verify не переопределяем (стандартное хранилище requests).
        # Content-Type с boundary requests выставляет сам.
        response = requests.post(upload_url,
                                 files={"data": (filename, media_data, content_type)},
                                 )
        if response.status_code != 200:
            raise IOError(f'{media_type} upload failed: {response.status_code} {response.text}')

        # Для картинок токен лежит в ответе загрузки: {"photos": {...: {"token": ...}}}
        try:
            result = response.json()
        except ValueError:
            result = {}
        photos = result.get('photos') or {}
        if photos:
            token = next(iter(photos.values()))['token']
        elif result.get('token'):
            token = result['token']

        if not token:
            raise IOError(f'{media_type} upload failed, no token in response: {response.text}')

        logger.info(f'MAX: {media_type} загружен: {filename}')
        return {"type": media_type, "payload": {"token": token}}

    # Отправка сообщения с медиафайлом (картинка, видео, аудио, файл)
    # и, если передана, клавиатурой управления.
    def send_media(self,
                   chat_id: int,
                   caption: str,
                   media: bytes | str,
                   media_type: str = 'image',
                   keyboard: dict | None = None) -> Message:
        attachments: List[Dict] = [self.upload_media(media, media_type)]
        if keyboard is not None:
            attachments.append(keyboard)
        return self.send_attachments(chat_id, caption, attachments)

    # Отправка картинки — частный случай send_media (метод базового класса Sender)
    def send_image(self,
                   chat_id: int,
                   caption: str,
                   image: bytes | str,
                   keyboard: dict | None = None) -> Message:
        return self.send_media(chat_id, caption, image, 'image', keyboard)

    # Отправка сообщения с уже готовыми вложениями (см. upload_media)
    @retry(retry_on_exception=retry_if_io_error,
           wait_exponential_multiplier=1000,
           wait_exponential_max=10000,
           stop_max_attempt_number=3,
           stop_max_delay=60000
           )
    def send_attachments(self, chat_id: int, caption: str, attachments: List[Dict]) -> Message:
        logger.info(f'MAX: Отправляем вложения: chat_id: {chat_id}, caption: {caption}, '
                    f'вложения: {[attachment["type"] for attachment in attachments]}')
        response = self._message_request('POST',
                                         params={"chat_id": chat_id},
                                         body={"text": caption, "attachments": attachments})
        if response.status_code != 200:
            raise IOError(f'Send message failed: {response.status_code} {response.text}')

        try:
            data = response.json()
        except ValueError as error:
            raise IOError(f'Send message response is not JSON: {error}')

        try:
            result = SendMessageResult.model_validate(data)
        except ValidationError as error:
            raise IOError(f'Не удалось разобрать ответ на отправку сообщения {data!r}: {error}')

        logger.info(f'MAX: сообщение отправлено, chat_id: {chat_id}, mid: {result.message.body.mid}')
        return result.message

    # Редактирование ранее отправленного сообщения (PUT /messages?message_id=...).
    # message_id  — mid сообщения (Message.body.mid).
    # text        — новый текст; None — не трогать текст.
    # attachments — новый список вложений; None — не трогать вложения,
    #               [] — убрать все вложения.
    # Так текстовая "заглушка" превращается в сообщение с картинкой/видео
    # и клавиатурой, а клавиатуру потом можно "отрезать" (см. without_keyboard).
    @retry(retry_on_exception=retry_if_io_error,
           wait_exponential_multiplier=1000,
           wait_exponential_max=10000,
           stop_max_attempt_number=3,
           stop_max_delay=60000
           )
    def edit_message(self,
                     message_id: str,
                     text: str | None = None,
                     attachments: List[Dict] | None = None) -> None:
        logger.info(f'MAX: Редактируем сообщение: mid: {message_id}, text: {text}')
        body: Dict = {}
        if text is not None:
            body["text"] = text
        if attachments is not None:
            body["attachments"] = attachments

        response = self._message_request('PUT', params={"message_id": message_id}, body=body)
        if response.status_code != 200:
            raise IOError(f'Edit message failed: {response.status_code} {response.text}')

        logger.info(f'MAX: сообщение отредактировано, mid: {message_id}')

    # Запрос к /messages с ожиданием готовности вложений.
    # Видео MAX обрабатывает не мгновенно: пока файл не готов, API отвечает
    # ошибкой attachment.not.ready — ждём и повторяем запрос.
    def _message_request(self, method: str, params: Dict, body: Dict) -> requests.Response:
        response = None
        for attempt in range(1, self.attachment_attempts + 1):
            response = requests.request(method,
                                        self.send_message_url,
                                        params=params,
                                        headers=self.headers,
                                        json=body,
                                        verify="max_root_ca.crt",
                                        )
            if response.status_code == 400 and 'attachment.not.ready' in response.text:
                logger.info(f'MAX: вложение ещё обрабатывается, ждём '
                            f'({attempt}/{self.attachment_attempts})')
                time.sleep(self.attachment_delay)
                continue
            break
        return response

    # Вложения без inline-клавиатуры: остаются только медиафайлы.
    # Результат годится для передачи в edit_message.
    @staticmethod
    def without_keyboard(attachments: List[Dict]) -> List[Dict]:
        return [attachment for attachment in attachments
                if attachment["type"] != "inline_keyboard"]

    # Получение апдейтов методом long polling.
    # marker  — маркер из предыдущего ответа; None — взять сохранённый self.marker,
    #           а при первом запросе — все ещё не полученные апдейты.
    # timeout — сколько секунд сервер держит соединение, ожидая события (0..90).
    # limit   — максимум апдейтов в одном ответе (1..1000).
    # types   — фильтр по типам: message_created, message_callback, bot_started, ...
    # Маркер для следующего запроса сохраняется в self.marker.
    @retry(retry_on_exception=retry_if_io_error,
           wait_exponential_multiplier=1000,
           wait_exponential_max=10000,
           stop_max_attempt_number=3,
           stop_max_delay=60000
           )
    def get_updates(self,
                    marker: int | None = None,
                    timeout: int = 30,
                    limit: int = 100,
                    types: List[str] | None = None) -> List[Update]:
        params: dict = {"timeout": timeout, "limit": limit}
        if marker is None:
            marker = self.marker
        # marker не передаём вовсе, если его ещё нет: тогда сервер отдаёт
        # все апдейты, которые бот ещё не получал.
        if marker is not None:
            params["marker"] = marker
        if types:
            params["types"] = ",".join(types)

        # Ответ ждём дольше, чем длится long polling на стороне сервера.
        response = requests.get(self.updates_url,
                                params=params,
                                headers=self.headers,
                                verify="max_root_ca.crt",
                                timeout=timeout + 10,
                                )
        if response.status_code != 200:
            raise IOError(f'Updates request failed: {response.status_code} {response.text}')

        try:
            data = response.json()
        except ValueError as error:
            raise IOError(f'Updates response is not JSON: {error}')

        self.marker = data.get('marker')

        # Один битый апдейт не должен ронять весь опрос: логируем и пропускаем его.
        updates: List[Update] = []
        for item in data.get('updates') or []:
            try:
                updates.append(Update.model_validate(item))
            except ValidationError as error:
                logger.error(f'MAX: не удалось разобрать апдейт {item!r}: {error}')

        if updates:
            logger.info(f'MAX: получено апдейтов: {len(updates)}, следующий маркер: {self.marker}')
        return updates

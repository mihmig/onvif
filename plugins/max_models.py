# Pydantic-модели апдейтов Bot API MAX.
# Описание структуры: https://dev.max.ru/#updates
# Примеры реальных данных: jsons/max_updates.json
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class User(BaseModel):
    """Пользователь (отправитель сообщения или автор нажатия на кнопку)."""
    model_config = ConfigDict(extra="allow")

    user_id: int
    name: str
    first_name: str
    last_name: Optional[str] = None
    username: Optional[str] = None
    is_bot: bool = False
    last_activity_time: int = 0


class Recipient(BaseModel):
    """Получатель сообщения (чат, в который доставлен апдейт)."""
    model_config = ConfigDict(extra="allow")

    chat_id: int
    chat_type: str  # dialog | group | channel
    user_id: Optional[int] = None


class PhotoPayload(BaseModel):
    """Payload вложения типа image."""
    model_config = ConfigDict(extra="allow")

    photo_id: Optional[int] = None
    token: Optional[str] = None
    url: Optional[str] = None


class InlineButton(BaseModel):
    """Кнопка inline-клавиатуры."""
    model_config = ConfigDict(extra="allow")

    text: str
    type: str = "callback"  # callback | link | ...
    payload: Optional[str] = None
    url: Optional[str] = None


class InlineKeyboardPayload(BaseModel):
    """Payload вложения типа inline_keyboard."""
    model_config = ConfigDict(extra="allow")

    buttons: list[list[InlineButton]] = []


class ContactPayload(BaseModel):
    """Payload вложения типа contact (карточка контакта)."""
    model_config = ConfigDict(extra="allow")

    max_info: Optional[User] = None


class Attachment(BaseModel):
    """Вложение сообщения. payload приводится к нужной модели по полю type."""
    model_config = ConfigDict(extra="allow")

    type: str  # image | inline_keyboard | contact | ...
    payload: Any = None

    @model_validator(mode="after")
    def _cast_payload(self) -> "Attachment":
        if isinstance(self.payload, dict):
            payload_class = {
                "image": PhotoPayload,
                "inline_keyboard": InlineKeyboardPayload,
                "contact": ContactPayload,
            }.get(self.type)
            if payload_class:
                self.payload = payload_class.model_validate(self.payload)
        return self


class MessageBody(BaseModel):
    """Тело сообщения: текст и вложения."""
    model_config = ConfigDict(extra="allow")

    mid: str
    seq: Optional[int] = None
    text: Optional[str] = None
    attachments: list[Attachment] = Field(default_factory=list)


class Message(BaseModel):
    """Сообщение (входящее или привязанное к callback)."""
    model_config = ConfigDict(extra="allow")

    recipient: Recipient
    timestamp: int
    body: MessageBody
    sender: User


class Callback(BaseModel):
    """Нажатие на callback-кнопку inline-клавиатуры."""
    model_config = ConfigDict(extra="allow")

    timestamp: int
    callback_id: str
    user: User
    payload: str


class Update(BaseModel):
    """Апдейт от Bot API MAX (элемент массива updates в ответе /get-updates)."""
    model_config = ConfigDict(extra="allow")

    update_type: str  # message_created | message_callback | user_added | bot_started | ...
    timestamp: int
    message: Optional[Message] = None
    callback: Optional[Callback] = None
    # user_added, user_removed, bot_started: участник, о котором апдейт
    user: Optional[User] = None
    # user_added: кто пригласил участника в чат
    inviter_id: Optional[int] = None
    user_locale: Optional[str] = None

    @property
    def chat_id(self) -> Optional[int]:
        """ID чата: у сообщений — из получателя, у остальных апдейтов — из корня."""
        if self.message is not None:
            return self.message.recipient.chat_id
        # user_added, user_removed, bot_started: chat_id лежит в самом апдейте
        return (self.model_extra or {}).get('chat_id')


class SendMessageResult(BaseModel):
    """Ответ Bot API MAX на отправку сообщения (POST /messages).

    Пример реальных данных: tests/send_message_answer.json
    """
    model_config = ConfigDict(extra="allow")

    message: Message

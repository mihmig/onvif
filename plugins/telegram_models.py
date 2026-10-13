# Pydantic-модели апдейтов Bot API Telegram.
# Описание структуры: https://core.telegram.org/bots/api#available-types
# Модели повторяют по смыслу plugins/max_models.py, но названия полей
# взяты из документации Telegram: у него своя схема сообщений и апдейтов.
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class User(BaseModel):
    """Пользователь или бот (https://core.telegram.org/bots/api#user)."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: int
    is_bot: bool = False
    first_name: str
    last_name: Optional[str] = None
    username: Optional[str] = None
    language_code: Optional[str] = None


class Chat(BaseModel):
    """Чат, в который доставлен апдейт (https://core.telegram.org/bots/api#chat)."""
    model_config = ConfigDict(extra="allow")

    id: int
    type: str  # private | group | supergroup | channel
    title: Optional[str] = None
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None


class PhotoSize(BaseModel):
    """Одна из версий картинки (https://core.telegram.org/bots/api#photosize).

    Telegram возвращает список размеров: от миниатюры до оригинала.
    """
    model_config = ConfigDict(extra="allow")

    file_id: str
    file_unique_id: str
    width: int
    height: int
    file_size: Optional[int] = None


class Video(BaseModel):
    """Видеофайл (https://core.telegram.org/bots/api#video)."""
    model_config = ConfigDict(extra="allow")

    file_id: str
    file_unique_id: str
    width: int
    height: int
    duration: int
    thumbnail: Optional[PhotoSize] = None
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    file_size: Optional[int] = None


class Document(BaseModel):
    """Файл, отправленный как документ (https://core.telegram.org/bots/api#document)."""
    model_config = ConfigDict(extra="allow")

    file_id: str
    file_unique_id: str
    thumbnail: Optional[PhotoSize] = None
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    file_size: Optional[int] = None


class InlineKeyboardButton(BaseModel):
    """Кнопка inline-клавиатуры (https://core.telegram.org/bots/api#inlinekeyboardbutton).

    В отличие от MAX, тип кнопки отдельным полем не передаётся: кнопка
    считается callback-кнопкой, если у неё заполнено callback_data.
    """
    model_config = ConfigDict(extra="allow")

    text: str
    callback_data: Optional[str] = None
    url: Optional[str] = None


class InlineKeyboardMarkup(BaseModel):
    """Inline-клавиатура (https://core.telegram.org/bots/api#inlinekeyboardmarkup)."""
    model_config = ConfigDict(extra="allow")

    inline_keyboard: list[list[InlineKeyboardButton]] = Field(default_factory=list)


class Message(BaseModel):
    """Сообщение (https://core.telegram.org/bots/api#message).

    Сообщение адресуется парой (chat.id, message_id): одного message_id,
    в отличие от mid в MAX, для редактирования и удаления не хватает.
    """
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    message_id: int
    # from — зарезервированное слово Python, поэтому поле читаем по алиасу
    from_user: Optional[User] = Field(default=None, alias="from")
    date: int
    chat: Chat
    text: Optional[str] = None
    caption: Optional[str] = None
    photo: list[PhotoSize] = Field(default_factory=list)
    video: Optional[Video] = None
    document: Optional[Document] = None
    reply_markup: Optional[InlineKeyboardMarkup] = None
    # Служебное сообщение о входе в группу: список вошедших участников.
    # Служебные сообщения бот получает всегда, независимо от режима приватности.
    new_chat_members: list[User] = Field(default_factory=list)

    @property
    def chat_id(self) -> int:
        """ID чата, в котором лежит сообщение."""
        return self.chat.id

    @property
    def has_media(self) -> bool:
        """Есть ли в сообщении медиа: от этого зависит способ редактирования."""
        return bool(self.photo) or self.video is not None or self.document is not None


class CallbackQuery(BaseModel):
    """Нажатие на callback-кнопку (https://core.telegram.org/bots/api#callbackquery).

    На каждый callback нужно ответить методом answerCallbackQuery, иначе
    у пользователя надолго повисает "часики" на кнопке.
    """
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    from_user: Optional[User] = Field(default=None, alias="from")
    message: Optional[Message] = None
    chat_instance: Optional[str] = None
    # Полезная нагрузка кнопки (в MAX это callback.payload)
    data: Optional[str] = None


class Update(BaseModel):
    """Апдейт Bot API Telegram (элемент массива result в ответе getUpdates).

    Описание: https://core.telegram.org/bots/api#update
    """
    model_config = ConfigDict(extra="allow")

    update_id: int
    message: Optional[Message] = None
    edited_message: Optional[Message] = None
    channel_post: Optional[Message] = None
    edited_channel_post: Optional[Message] = None
    callback_query: Optional[CallbackQuery] = None

    @property
    def update_type(self) -> str:
        """Тип апдейта: Telegram отдельным полем его не присылает,
        тип определяется тем, какое поле заполнено."""
        for name in ("message", "edited_message", "channel_post",
                     "edited_channel_post", "callback_query"):
            if getattr(self, name) is not None:
                return name
        return "unknown"

    @property
    def chat_id(self) -> Optional[int]:
        """ID чата: из сообщения, а для нажатия на кнопку — из сообщения с клавиатурой."""
        message = self.message or self.edited_message or self.channel_post \
                  or self.edited_channel_post
        if message is None and self.callback_query is not None:
            message = self.callback_query.message
        return message.chat.id if message else None


class Response(BaseModel):
    """Конверт любого ответа Bot API: {"ok": true, "result": ...}.

    При ошибке ok = false, а описание лежит в description и error_code.
    """
    model_config = ConfigDict(extra="allow")

    ok: bool
    result: Any = None
    description: Optional[str] = None
    error_code: Optional[int] = None


class MessageResult(Response):
    """Ответ на отправку/редактирование сообщения: в result лежит Message."""

    result: Message

# Тот же бот управления камерой, что и в worker_max.py, но для Telegram.
# Логика полностью совпадает, отличается только работа с API мессенджера:
# - сообщение адресуется парой (chat_id, message_id);
# - текстовую заглушку нельзя превратить в медиа, поэтому вместо
#   редактирования она удаляется, а медиа уходит новым сообщением;
# - на каждое нажатие кнопки нужно ответить answerCallbackQuery.
# Кроме кнопок бот отвечает снимком на вход нового участника в группу
# и на команду /start, поздоровавшись перед этим. Администраторам из
# admin_ids в config.yml доступна команда /log <уровень> — смена
# уровня логирования на лету.
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import yaml

from cameras.camera import CameraOffline
from cameras.factory import create_camera, find_camera_config
from cameras.menu import make_telegram_keyboard, ptz_directions
from plugins.telegram_models import Update, User
from plugins.telegram_sender import TelegramSender

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(filename)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger(__name__)

with open('config/config.yml', encoding='utf-8') as file:
    config = yaml.safe_load(file)


camera_id = sys.argv[1] if len(sys.argv) > 1 else ''
camera_config = find_camera_config(config['cameras'], camera_id)
camera = create_camera(camera_config)
profile_token = camera_config["profile_token"]

telegram_sender = TelegramSender(camera_config)
# Идентификаторы чатов у Telegram и MAX разные, поэтому телеграмный
# берём из telegram_chat_id, а chat_id оставляем как запасной вариант.
chat_id: int = camera_config.get('telegram_chat_id') or camera_config.get('chat_id')
if not chat_id:
    raise SystemExit('Не задан telegram_chat_id (или chat_id) в config/config.yml')

# Администраторы из config.yml: им разрешено менять уровень логирования
admin_ids: set = set(config.get('admin_ids') or [])

ptz_speed: float = camera_config['ptz_speed']
ptz_duration: float = camera_config['ptz_duration']
video_duration: float = camera_config.get('video_duration', 10.0)
video_dir: str = camera_config.get('video_dir', 'data')

# Клавиатуру собираем под возможности камеры: PTZ-матрица и/или второй объектив.
# Длительность ролика подписана на кнопке видео.
keyboard = make_telegram_keyboard(video_duration, camera.has_ptz, camera.has_second_lens)


@dataclass
class Screen:
    """
    Сообщение-«экран» бота: медиа с клавиатурой управления.
    Храним то, что сами положили в сообщение: при редактировании
    нужно знать, правим мы текст или подпись к медиа.
    """
    message_id: int
    text: str
    has_media: bool = False


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def user_name(user: Optional[User]) -> str:
    """Имя пользователя для приветствия: имя и фамилия, иначе @username."""
    if user is None:
        return ''
    name = ' '.join(part for part in (user.first_name, user.last_name) if part)
    if name:
        return name
    return f'@{user.username}' if user.username else ''


def welcome_text(names: str) -> str:
    """Приветствие новому участнику группы или тому, кто прислал /start."""
    hello = f'Здравствуйте, {names}!' if names else 'Здравствуйте!'
    return (f'{hello}\n'
            f'Это бот камеры «{camera_config.get("name") or "камера"}». '
            f'Кнопками под снимком можно поворачивать камеру, '
            f'делать фото и записывать видео ({video_duration:g} с.).')


def is_start_command(text: str | None) -> bool:
    """Команда /start. В группе она приходит с именем бота: /start@camera_bot."""
    first_word = text.strip().split(' ')[0] if text else ''
    return first_word.split('@')[0] == '/start'


def handle_log_command(update: Update) -> bool:
    """Команда /log от администратора: меняет уровень логирования на лету.

    /log — показать текущий уровень, /log debug|info|warning|error — поменять.
    Возвращает True, если апдейт был этой командой: дальше его не обрабатываем.
    """
    message = update.message
    if message is None or message.from_user is None:
        return False

    # Апдейты, накопившиеся до запуска, пропускаем: как и остальные команды
    if message.date < started_at:
        return False

    words = (message.text or '').strip().split(' ')
    if not words or words[0].split('@')[0] != '/log':
        return False

    user = message.from_user
    if user.id not in admin_ids:
        logger.warning("/log от постороннего пользователя %s (id %s), игнорируем",
                       user_name(user), user.id)
        telegram_sender.send_message(chat_id, 'Менять уровень логирования может только администратор')
        return True

    if len(words) < 2:
        current = logging.getLevelName(logging.getLogger().getEffectiveLevel())
        telegram_sender.send_message(chat_id,
                                     f'Текущий уровень логирования: {current}\n'
                                     f'Доступные: DEBUG, INFO, WARNING, ERROR')
        return True

    level_name = words[1].upper()
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        telegram_sender.send_message(chat_id,
                                     f'Неизвестный уровень «{words[1]}»\n'
                                     f'Доступные: DEBUG, INFO, WARNING, ERROR')
        return True

    # basicConfig настраивает корневой логгер — меняем уровень у него,
    # тогда он подействует на все логгеры проекта сразу
    logging.getLogger().setLevel(level)
    logger.info("Пользователь %s (id %s) сменил уровень логирования на %s",
                user_name(user), user.id, level_name)
    telegram_sender.send_message(chat_id, f'Уровень логирования: {level_name}')
    return True


# Команды медиа: фото/видео с первого объектива и со второго ('...-2'),
# снимок с обоих объективов сразу ('make-foto-all')
MEDIA_COMMANDS = ('make-foto', 'make-video', 'make-foto-2', 'make-video-2', 'make-foto-all')


def request_text(name: str, command: str) -> str:
    """Заглушка на время выполнения: кто и что запросил."""
    if command in ('make-video', 'make-video-2'):
        what = f'видео ({video_duration:g} с.)'
    elif command == 'make-foto-all':
        what = 'фото [1+2]'
    elif command in ('make-foto', 'make-foto-2'):
        what = 'фото'
    else:
        what = 'поворот камеры'
    if command.endswith('-2'):
        what += ' [2]'  # второй объектив
    return f'Пользователь {name or "без имени"} запросил {what}, выполняем...'


def command_from_update(update: Update) -> Tuple[str | None, str | None, str]:
    """Разбирает апдейт в тройку (команда клавиатуры, приветствие, имя пользователя).

    Команда None — апдейт нам не интересен. Приветствие не None — перед
    снимком надо поздороваться: пришёл новый участник или команда /start.
    Нажатие кнопки подтверждаем сразу здесь: ответить нужно быстро.
    """
    if update.callback_query is not None:
        callback = update.callback_query
        # Отвечаем сразу, иначе на кнопке надолго останутся «часики»
        telegram_sender.answer_callback_query(callback.id)
        return callback.data, None, user_name(callback.from_user)

    message = update.message
    if message is None:
        return None, None, ''

    # Апдейты, накопившиеся до запуска, пропускаем: иначе вчерашняя команда
    # заставит бота снимать сразу после старта.
    if message.date < started_at:
        logger.info("Пропускаем сообщение от %s: оно старше запуска бота",
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(message.date)))
        return None, None, ''

    # Новый участник группы: здороваемся и показываем, что видит камера.
    # Ботов (в том числе самого себя при добавлении в группу) не приветствуем.
    people = [user for user in message.new_chat_members if not user.is_bot]
    if people:
        names = ', '.join(name for name in map(user_name, people) if name)
        return 'make-foto', welcome_text(names), names

    # Команда /start. Если у бота включён режим приватности, в группе
    # он получит её только в виде /start@имя_бота.
    if is_start_command(message.text):
        name = user_name(message.from_user)
        return 'make-foto', welcome_text(name), name

    return None, None, ''


def show_status(text: str) -> Screen:
    """Текстовая заглушка на время запроса данных с камеры."""
    return Screen(telegram_sender.send_message(chat_id, text).message_id, text)


def drop_screen(screen: Screen) -> None:
    """Удаляет сообщение-заглушку. Не удалилось — не страшно, идём дальше."""
    try:
        telegram_sender.delete_message(chat_id, screen.message_id)
    except Exception as error:
        logger.error("Не удалось удалить сообщение %s: %s", screen.message_id, error)


def fill_screen(screen: Screen, text: str, media: bytes | str, media_type: str) -> Screen:
    """Подменяет заглушку на медиа с клавиатурой управления.

    Telegram не умеет превращать текстовое сообщение в медиа, поэтому
    сначала отправляем медиа, а потом убираем заглушку: если отправка
    не удастся, пользователь останется хотя бы с текстом.
    """
    try:
        message = telegram_sender.send_media(chat_id, text, media, media_type, keyboard)
    except Exception as error:
        # Telegram отклонил запрос (например, медиа слишком большое) —
        # сообщаем об этом в заглушке, но бота не роняем
        logger.error("Не удалось отправить медиа: %s", error)
        return fail_screen(screen, f'Не удалось отправить данные в Telegram: {error}')
    drop_screen(screen)
    return Screen(message.message_id, text, True)


def fail_screen(screen: Screen, text: str) -> Screen:
    """Сообщает об ошибке, но оставляет клавиатуру: управление не теряем.

    Не отредактировалось (например, Telegram недоволен запросом) — не страшно:
    логируем и идём дальше, ронять бота из-за одного сообщения нельзя.
    """
    try:
        telegram_sender.edit_message(chat_id, screen.message_id,
                                     text=text, keyboard=keyboard, has_media=screen.has_media)
    except Exception as error:
        logger.error("Не удалось изменить сообщение %s: %s", screen.message_id, error)
    return Screen(screen.message_id, text, screen.has_media)


def cut_keyboard(screen: Screen) -> None:
    """Убирает клавиатуру у сообщения: остаётся только картинка/видео.

    Не убралась — не страшно: обработку команды продолжаем в любом случае.
    """
    try:
        telegram_sender.edit_message(chat_id, screen.message_id, keyboard=None)
    except Exception as error:
        logger.error("Не удалось убрать клавиатуру у сообщения %s: %s", screen.message_id, error)


def caption(text: str, name: str) -> str:
    """Подпись к медиа: добавляем, кто его запросил (если знаем)."""
    return f'{text}, запросил {name}' if name else text


def show_snapshot(status: str, direction: Tuple[float, float] | None = None,
                  name: str = '', lens: int = 1) -> Screen:
    """Заглушка -> (поворот камеры) -> снимок -> сообщение со снимком и клавиатурой.

    lens: 1 — основной объектив (ONVIF), 2 — второй (статический) у сдвоенной камеры,
    'all' — оба объектива, склеенные вертикально в один снимок.
    """
    screen = show_status(status)
    try:
        if direction is not None:
            x, y = direction
            camera.ptz_move(profile_token, x * ptz_speed, y * ptz_speed, ptz_duration)
            time.sleep(ptz_duration)  # Даём время на остановку камеры
        if lens == 'all':
            image = camera.get_all_image(profile_token)
        elif lens == 2:
            image = camera.get_second_image()
        else:
            image = camera.get_image(profile_token)
    except CameraOffline as error:
        logger.error("Камера не в сети: %s", error)
        return fail_screen(screen, f'Камера не в сети ({error}), попробуйте ещё раз')
    except Exception as error:
        logger.error("Не удалось получить снимок: %s", error)
        return fail_screen(screen, f'Не удалось получить снимок с камеры: {error}')

    return fill_screen(screen, caption(f'Снимок от {now()}', name), image, 'image')


def show_video(status: str, name: str = '', lens: int = 1) -> Screen:
    """Заглушка -> запись ролика -> сообщение с видео и клавиатурой.

    lens: 1 — основной объектив (ONVIF), 2 — второй (статический) у сдвоенной камеры.
    """
    screen = show_status(status)
    filename = os.path.join(video_dir, f'video_{time.strftime("%Y-%m-%d-%H-%M-%S")}.mp4')
    try:
        os.makedirs(video_dir, exist_ok=True)
        if lens == 2:
            path = camera.get_second_video(video_duration, filename)
        else:
            path = camera.get_video(profile_token, video_duration, filename)
    except CameraOffline as error:
        logger.error("Камера не в сети: %s", error)
        return fail_screen(screen, f'Камера не в сети ({error}), попробуйте ещё раз')
    except Exception as error:
        logger.error("Не удалось записать видео: %s", error)
        return fail_screen(screen, f'Не удалось записать видео с камеры: {error}')

    if path is None:
        return fail_screen(screen, 'Не удалось записать видео с камеры')

    return fill_screen(screen, caption(f'Видео от {now()} ({video_duration:g} с.)', name), path, 'video')


# Пауза перед повторным опросом Telegram, если запрос обновлений не удался
updates_retry_delay: float = 10.0

# Время запуска: сообщения, отправленные раньше, бот не обрабатывает.
# Telegram хранит неподтверждённые апдейты сутки и отдаёт их все сразу.
started_at: int = int(time.time())

# Текущий экран: пока он есть, клавиатура висит именно на нём
screen: Screen | None = None

try:
    screen = show_snapshot(f'Бот запущен {now()}, запрашиваем данные с камеры...')
# except Exception as error:
#     # Стартовый снимок не получился/не отправился — но бот должен работать дальше
#     logger.exception("Не удалось показать стартовый снимок: %s", error)

    while True:
        try:
            updates: List[Update] = telegram_sender.get_updates()
        except Exception as error:
            # Пропала сеть или Telegram не отвечает — ждём и пробуем ещё раз
            logger.error("Не удалось получить обновления: %s", error)
            time.sleep(updates_retry_delay)
            continue

        # Пустой ответ при long polling — обычное дело, не засоряем лог:
        # пишем только когда действительно что-то пришло
        if updates:
            logger.info("Получено update: %s", len(updates))
        else:
            logger.debug("Обновлений нет")
        for update in updates:
            if update.chat_id != chat_id:
                continue

            # Служебная команда /log: смена уровня логирования (только администраторам)
            try:
                if handle_log_command(update):
                    continue
            except Exception as error:
                logger.error("Не удалось обработать команду /log: %s", error)

            # Нажатие кнопки, вход нового участника или /start
            command, welcome, name = command_from_update(update)
            if command is None:
                continue
            logger.info("Получена команда %s от %s", command, name)

            direction = ptz_directions.get(command)
            if direction is None and command not in MEDIA_COMMANDS:
                # Незнакомую команду игнорируем.
                # Клавиатуру при этом не трогаем: иначе управлять станет нечем.
                logger.info("Команда %s пока не поддерживается, пропускаем", command)
                continue

            if welcome is not None:
                # Приветствие остаётся в чате отдельным сообщением: экран
                # со снимком живёт своей жизнью и будет заменён следующим
                try:
                    telegram_sender.send_message(chat_id, welcome)
                except Exception as error:
                    # Не поздоровались — не повод отказываться от снимка
                    logger.error("Не удалось отправить приветствие: %s", error)

            # Команда принята, клавиатура своё отработала — убираем её
            # у текущего экрана, остаётся только картинка/видео
            if screen is not None:
                cut_keyboard(screen)
                screen = None

            try:
                status = request_text(name, command)
                if command == 'make-video':
                    screen = show_video(status, name)
                elif command == 'make-video-2':
                    screen = show_video(status, name, lens=2)
                elif command == 'make-foto-2':
                    screen = show_snapshot(status, name=name, lens=2)
                elif command == 'make-foto-all':
                    screen = show_snapshot(status, name=name, lens='all')
                else:
                    # make-foto или PTZ-поворот: снимок с первого объектива
                    screen = show_snapshot(status, direction, name)
            except Exception as error:
                # Не роняем бота из-за одной команды, но и без клавиатуры не оставляем
                logger.exception("Ошибка при обработке команды %s: %s", command, error)
                try:
                    message = telegram_sender.send_message(chat_id, f'Ошибка: {error}', keyboard=keyboard)
                    screen = Screen(message.message_id, f'Ошибка: {error}')
                except Exception as send_error:
                    # Даже это не отправилось — просто ждём следующей команды
                    logger.error("Не удалось отправить сообщение об ошибке: %s", send_error)

except KeyboardInterrupt:
    logger.info("Получен Ctrl+C, завершаем работу...")

finally:
    # Клавиатуру у последнего сообщения убираем: нажимать её больше некому
    if screen is not None:
        try:
            telegram_sender.edit_message(chat_id, screen.message_id,
                                         text=f'{screen.text}\n\nБот остановлен {now()}',
                                         keyboard=None,
                                         has_media=screen.has_media)
        except Exception as error:
            logger.error("Не удалось убрать клавиатуру при остановке: %s", error)
    logger.info("Работа завершена.")

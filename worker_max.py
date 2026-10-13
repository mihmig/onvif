import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import yaml

from cameras.camera import CameraOffline
from cameras.factory import create_camera, find_camera_config
from cameras.menu import make_keyboard, ptz_directions
from plugins.max_models import Update, User
from plugins.max_sender import MaxSender

logging.basicConfig(level=logging.INFO, format='%(asctime)s|%(filename)s|%(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger(__name__)


with open('config/config.yml') as file:
    config = yaml.safe_load(file)

camera_id = sys.argv[1] if len(sys.argv) > 1 else ''
camera_config = find_camera_config(config['cameras'], camera_id)

camera = create_camera(camera_config)
profile_token = camera_config["profile_token"]

max_sender = MaxSender(camera_config)
chat_id: int = camera_config['chat_id']

# Администраторы из config.yml: им разрешено менять уровень логирования
admin_ids: set = set(config.get('admin_ids') or [])

ptz_speed: float = camera_config['ptz_speed']
ptz_duration: float = camera_config['ptz_duration']
video_duration: float = camera_config.get('video_duration', 10.0)
video_dir: str = camera_config.get('video_dir', 'data')

# Клавиатуру собираем под возможности камеры: PTZ-матрица и/или второй объектив.
# Длительность ролика подписана на кнопке видео.
keyboard = make_keyboard(video_duration, camera.has_ptz, camera.has_second_lens)


@dataclass
class Screen:
    """
    Сообщение-«экран» бота: медиа с клавиатурой управления.
    Храним то, что сами положили в сообщение: при редактировании
    API новую версию сообщения не возвращает.
    """
    mid: str
    text: str
    attachments: List[Dict] = field(default_factory=list)


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def user_name(user: Optional[User]) -> str:
    """Имя пользователя для приветствия: имя и фамилия, иначе @username."""
    if user is None:
        return ''
    name = ' '.join(part for part in (user.first_name, user.last_name) if part) or user.name
    if name:
        return name
    return f'@{user.username}' if user.username else ''


def welcome_text(names: str) -> str:
    """Приветствие новому участнику группы или тому, кто запустил бота."""
    hello = f'Здравствуйте, {names}!' if names else 'Здравствуйте!'
    return (f'{hello}\n'
            f'Это бот камеры «{camera_config.get("name") or "камера"}». '
            f'Кнопками под снимком можно поворачивать камеру, '
            f'делать фото и записывать видео ({video_duration:g} с.).')


def is_start_command(text: str | None) -> bool:
    """Команда /start. В группе она может прийти с именем бота: /start@camera_bot."""
    first_word = text.strip().split(' ')[0] if text else ''
    return first_word.split('@')[0] == '/start'


def handle_log_command(update: Update) -> bool:
    """Команда /log от администратора: меняет уровень логирования на лету.

    /log — показать текущий уровень, /log debug|info|warning|error — поменять.
    Возвращает True, если апдейт был этой командой: дальше его не обрабатываем.
    """
    if update.update_type != 'message_created' or update.message is None:
        return False

    # Апдейты, накопившиеся до запуска, пропускаем: как и остальные команды
    if update.timestamp < started_at:
        return False

    words = (update.message.body.text or '').strip().split(' ')
    if not words or words[0].split('@')[0] != '/log':
        return False

    user = update.message.sender
    if user.user_id not in admin_ids:
        logger.warning("/log от постороннего пользователя %s (id %s), игнорируем",
                       user_name(user), user.user_id)
        max_sender.send_message(chat_id, 'Менять уровень логирования может только администратор')
        return True

    if len(words) < 2:
        current = logging.getLevelName(logging.getLogger().getEffectiveLevel())
        max_sender.send_message(chat_id,
                                f'Текущий уровень логирования: {current}\n'
                                f'Доступные: DEBUG, INFO, WARNING, ERROR')
        return True

    level_name = words[1].upper()
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        max_sender.send_message(chat_id,
                                f'Неизвестный уровень «{words[1]}»\n'
                                f'Доступные: DEBUG, INFO, WARNING, ERROR')
        return True

    # basicConfig настраивает корневой логгер — меняем уровень у него,
    # тогда он подействует на все логгеры проекта сразу
    logging.getLogger().setLevel(level)
    logger.info("Пользователь %s (id %s) сменил уровень логирования на %s",
                user_name(user), user.user_id, level_name)
    max_sender.send_message(chat_id, f'Уровень логирования: {level_name}')
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
    """
    if update.update_type == 'message_callback' and update.callback is not None:
        return update.callback.payload, None, user_name(update.callback.user)

    # Апдейты, накопившиеся до запуска, пропускаем: иначе вчерашнее событие
    # заставит бота снимать сразу после старта. Время в MAX — в миллисекундах.
    if update.timestamp < started_at:
        logger.info("Пропускаем апдейт %s: он старше запуска бота", update.update_type)
        return None, None, ''

    # Новый участник группы или тот, кто только что запустил бота: здороваемся
    # и показываем, что видит камера. Ботов (в том числе самого себя при
    # добавлении в группу) не приветствуем.
    if update.update_type in ('user_added', 'bot_started'):
        if update.user is None or update.user.is_bot:
            return None, None, ''
        name = user_name(update.user)
        return 'make-foto', welcome_text(name), name

    # Команда /start текстом
    if update.update_type == 'message_created' and update.message is not None:
        if is_start_command(update.message.body.text) and not update.message.sender.is_bot:
            name = user_name(update.message.sender)
            return 'make-foto', welcome_text(name), name

    return None, None, ''


def show_status(text: str) -> Screen:
    """Текстовая заглушка на время запроса данных с камеры."""
    return Screen(max_sender.send_message(chat_id, text).body.mid, text)


def fill_screen(screen: Screen, text: str, media: bytes | str, media_type: str) -> Screen:
    """Подменяет заглушку на медиа с клавиатурой управления."""
    attachments = [max_sender.upload_media(media, media_type), keyboard]
    max_sender.edit_message(screen.mid, text=text, attachments=attachments)
    return Screen(screen.mid, text, attachments)


def fail_screen(screen: Screen, text: str) -> Screen:
    """Сообщает об ошибке, но оставляет клавиатуру: управление не теряем."""
    max_sender.edit_message(screen.mid, text=text, attachments=[keyboard])
    return Screen(screen.mid, text, [keyboard])


def cut_keyboard(screen: Screen) -> None:
    """Убирает клавиатуру у сообщения: остаётся только картинка/видео."""
    max_sender.edit_message(screen.mid,
                            text=screen.text,
                            attachments=max_sender.without_keyboard(screen.attachments))


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


# Пауза перед повторным опросом MAX, если запрос обновлений не удался
updates_retry_delay: float = 10.0

# Время запуска бота в миллисекундах: апдейты старше него не обрабатываем
started_at: int = int(time.time() * 1000)

# Текущий экран: пока он есть, клавиатура висит именно на нём
screen: Screen | None = None

try:
    screen = show_snapshot(f'Бот запущен {now()}, запрашиваем данные с камеры...')

    while True:
        try:
            updates: List[Update] = max_sender.get_updates()
        except Exception as error:
            # Пропала сеть или MAX не отвечает — ждём и пробуем ещё раз
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
                    max_sender.send_message(chat_id, welcome)
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
                message = max_sender.send_attachments(chat_id, f'Ошибка: {error}', [keyboard])
                screen = Screen(message.body.mid, f'Ошибка: {error}', [keyboard])

except KeyboardInterrupt:
    logger.info("Получен Ctrl+C, завершаем работу...")

finally:
    # Клавиатуру у последнего сообщения убираем: нажимать её больше некому
    if screen is not None:
        try:
            max_sender.edit_message(screen.mid,
                                    text=f'{screen.text}\n\nБот остановлен {now()}',
                                    attachments=max_sender.without_keyboard(screen.attachments))
        except Exception as error:
            logger.error("Не удалось убрать клавиатуру при остановке: %s", error)
    logger.info("Работа завершена.")

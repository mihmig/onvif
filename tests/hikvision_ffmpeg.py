# https://github.com/nirsimetri/onvif-python
import os
import shutil
import subprocess
import time
from urllib.parse import quote, urlparse, urlunparse

import yaml
from onvif import ONVIFClient

with open('../config/config.yml') as file:
    config = yaml.safe_load(file)

camera = config['cameras'][0]

# Путь к ffmpeg (можно переопределить в config.yml ключом ffmpeg)
FFMPEG = config.get('ffmpeg', 'ffmpeg')


def get_stream_uri(media_service, profile_token) -> str:
    """Возвращает RTSP-ссылку для указанного профиля"""
    uri = media_service.GetStreamUri(
        StreamSetup={'Stream': 'RTP-Unicast', 'Transport': {'Protocol': 'RTSP'}},
        ProfileToken=profile_token,
    )
    return uri.Uri


def add_credentials(uri: str, user: str, password: str) -> str:
    """Подставляет логин/пароль в RTSP-ссылку: rtsp://user:pass@ip:554/..."""
    parts = urlparse(uri)
    host = parts.hostname or camera['camera']
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunparse(parts._replace(netloc=netloc))


def record_video(cam, profile_token, duration, filename=None, transport='tcp', audio=False):
    """
    Сохраняет видеоролик длительностью `duration` секунд из RTSP-потока камеры.

    cam:           клиент ONVIFClient
    profile_token: токен медиа-профиля (например, 'Profile_1')
    duration:      длительность ролика в секундах
    filename:      имя файла (по умолчанию data/rec_<token>_<timestamp>.mp4)
    transport:     транспорт RTSP: 'tcp' (надёжнее) или 'udp'
    audio:         True — писать звук (перекодируется в AAC), False — только видео

    Возвращает путь к сохранённому файлу или None при ошибке.
    """
    if shutil.which(FFMPEG) is None:
        print(f"Не найден ffmpeg ('{FFMPEG}'). Установите его или укажите путь в config.yml (ключ ffmpeg).")
        return None

    if filename is None:
        filename = f"data/rec_{profile_token}_{int(time.time())}.mp4"

    directory = os.path.dirname(filename)
    if directory:
        os.makedirs(directory, exist_ok=True)

    try:
        uri = get_stream_uri(cam.media(), profile_token)
    except Exception as e:
        print(f"Не удалось получить RTSP-ссылку для профиля {profile_token}: {e}")
        return None

    print(f"RTSP URL: {uri}")
    source = add_credentials(uri, camera['user'], camera['pass'])

    # -c:v copy — поток пишется без перекодирования (без нагрузки на CPU)
    cmd = [
        FFMPEG, '-hide_banner', '-loglevel', 'error', '-y',
        '-rtsp_transport', transport,
        '-i', source,
        '-t', str(duration),
        '-c:v', 'copy',
    ]
    cmd += ['-c:a', 'aac'] if audio else ['-an']
    # Если звук нужен — -c:a aac перекодирует аудио в AAC
    # (камеры часто отдают G.711/PCMU, а MP4 такой кодек не принимает,
    # поэтому здесь copy не подходит и CPU всё же тратится, но на аудио это копейки).
    # Если не нужен — -an полностью выбрасывает аудиодорожку.
    cmd += ['-movflags', '+faststart', filename]

    print(f"Запись {duration} сек. в '{filename}'...")
    try:
        # Запас по времени: подключение к камере + сброс буферов
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=duration + 30)
    except subprocess.TimeoutExpired:
        print("Превышено время ожидания ffmpeg, запись прервана.")
        return None

    if result.returncode != 0 or not os.path.exists(filename):
        print(f"Ошибка записи (код {result.returncode}): {result.stderr.strip()}")
        return None

    size_mb = os.path.getsize(filename) / (1024 * 1024)
    print(f"Видео успешно сохранено: '{filename}' ({size_mb:.2f} МБ)")
    return filename


if __name__ == '__main__':
    try:
        # Инициализация камеры

        client = ONVIFClient(camera['camera'], camera['port'], camera['user'], camera['pass'])
        # 1. Проверка системного времени (базовый тест связи)
        dt = client.devicemgmt().GetSystemDateAndTime()
        print(f" Успешное подключение! Время на камере: {dt.LocalDateTime.Time.Hour}:{dt.LocalDateTime.Time.Minute}")

        # 2. Получение информации об устройстве
        info = client.devicemgmt().GetDeviceInformation()
        print(f"Модель: {info.Model}")
        print(f"Прошивка: {info.FirmwareVersion}")

        # 3. Получение медиа-профилей (ссылок на потоки)
        media = client.media()
        profiles = media.GetProfiles()

        print("\nДоступные ONVIF профили:")
        for profile in profiles:
            # Запрос RTSP ссылки для каждого профиля
            print(f"- Профиль: {profile.Name} (Token: {profile.token})")
            print(f"  RTSP URL: {get_stream_uri(media, profile.token)}")
        # 4.

        # 5. Запись ролика из выбранного профиля
        # print()
        # record_video(client, profiles[0].token, duration=10)

    except Exception as e:
        print(f"Ошибка подключения или аутентификации: {e}")

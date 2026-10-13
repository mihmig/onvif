# Вариант записи без ffmpeg и без PyAV: чистый Python (см. rtsp_recorder.py).
# ONVIF нужен только чтобы узнать RTSP-ссылку профиля; на роутере ссылку
# можно захардкодить и вызывать rtsp_recorder.record_rtsp() напрямую.
import time

import yaml
from onvif import ONVIFClient

from rtsp_recorder import record_rtsp

with open('../config/config.yml') as file:
    config = yaml.safe_load(file)

camera = config['cameras'][0]


def get_stream_uri(media_service, profile_token) -> str:
    """Возвращает RTSP-ссылку для указанного профиля"""
    uri = media_service.GetStreamUri(
        StreamSetup={'Stream': 'RTP-Unicast', 'Transport': {'Protocol': 'RTSP'}},
        ProfileToken=profile_token,
    )
    return uri.Uri


def record_video(cam, profile_token, duration, filename=None, timeout=10):
    """
    Сохраняет видеоролик длительностью `duration` секунд из RTSP-потока камеры.
    Кадры перекладываются из RTP в MP4 без декодирования, внешние программы
    и сторонние библиотеки не нужны.

    cam:           клиент ONVIFClient
    profile_token: токен медиа-профиля (например, 'Profile_1')
    duration:      длительность ролика в секундах
    filename:      имя файла (по умолчанию data/rec_<token>_<timestamp>.mp4);
                   расширение .h264 даёт сырой поток вместо MP4

    Возвращает путь к сохранённому файлу или None при ошибке.
    """
    if filename is None:
        filename = f"data/rec_{profile_token}_{int(time.time())}.mp4"

    try:
        uri = get_stream_uri(cam.media(), profile_token)
    except Exception as e:
        print(f"Не удалось получить RTSP-ссылку для профиля {profile_token}: {e}")
        return None

    print(f"RTSP URL: {uri}")
    return record_rtsp(uri, duration, filename,
                       user=camera['user'], password=camera['pass'], timeout=timeout)


if __name__ == '__main__':
    try:
        client = ONVIFClient(camera['camera'], camera['port'], camera['user'], camera['pass'])

        info = client.devicemgmt().GetDeviceInformation()
        print(f"Модель: {info.Model}")
        print(f"Прошивка: {info.FirmwareVersion}")

        profiles = client.media().GetProfiles()
        print("\nДоступные ONVIF профили:")
        for profile in profiles:
            print(f"- Профиль: {profile.Name} (Token: {profile.token})")

        print()
        record_video(client, profiles[0].token, duration=10)

    except Exception as e:
        print(f"Ошибка подключения или аутентификации: {e}")

import requests
import time

import yaml
from requests.auth import HTTPDigestAuth
from onvif import ONVIFClient

with open('config/config.yml') as file:
    config = yaml.safe_load(file)

camera = config['cameras'][0]

try:
    client = ONVIFClient(camera['host'], camera['port'], camera['user'], camera['pass'])
    media = client.media()

    profile = media.GetProfiles()[0]  # use the first profile
    print("Profile Name:", profile.Name)

    image_uri = media.GetSnapshotUri(ProfileToken=profile.token)
    print("Snapshot URI:", image_uri.Uri)
    uri = image_uri.Uri
    print(f"Пытаемся скачать скриншот по URL: {uri}")

    # Скачиваем изображение с поддержкой Digest-авторизации Hikvision
    response = requests.get(uri, auth=HTTPDigestAuth(camera['user'], camera['pass']), timeout=5)

    filename = f"data/hikvision_{time.strftime("%Y-%m-%d-%H-%M-%S")}.jpg"
    if response.status_code == 200:
        with open(filename, 'wb') as f:
            f.write(response.content)
        print(f"Скриншот успешно сохранен как '{filename}'")
    else:
        print(f"Не удалось скачать кадр. Код ответа: {response.status_code}")
except Exception as e:
    print(e)
import logging
import os
import time

import yaml

from cameras.hikvision import Hikvision

# logging.basicConfig(level=logging.INFO)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(filename)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

with open('config/config.yml') as file:
    config = yaml.safe_load(file)

camera_config = config['cameras'][0]

camera = Hikvision(camera_config)
profile_token = camera_config["profile_token"]
# PTZ
camera.ptz_move(profile_token, 1.0, 0, 1)
time.sleep(1) # Обязательно, иначе движение смазано
# Изображение
image_data = camera.get_image(profile_token)
filename = f"data/hikvision_{time.strftime("%Y-%m-%d-%H-%M-%S")}.jpg"
with open(filename, 'wb') as f:
    f.write(image_data)

# Видео
# filename = f"data/hikvision_{time.strftime("%Y-%m-%d-%H-%M-%S")}.mp4"
# directory = os.path.dirname(filename)
# if directory:
#     os.makedirs(directory, exist_ok=True)
# video_data = camera.get_video(profile_token, 3.0, filename)
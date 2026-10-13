import sys
import time
import requests
import yaml
from onvif import ONVIFClient
from requests.auth import HTTPDigestAuth

with open('../config/config.yml') as file:
    config = yaml.safe_load(file)

camera = config['cameras'][0]
# Константы для PTZ
MOVE_DURATION = 0.8  # Время движения в секундах после одного нажатия
PTZ_SPEED = 0.5      # Скорость движения (от 0.1 до 1.0)
auth = HTTPDigestAuth(camera['user'], camera['pass'])


def create_camera() -> ONVIFClient:
    try:
        client = ONVIFClient(camera['camera'], camera['port'], camera['user'], camera['pass'])
        return client
    except Exception as e:
        print(f"Ошибка инициализации камеры: {e}")
        return None


def capture_screenshot(cam, profile_token, filename="screenshot.jpg"):
    """Получает URI скриншота через ONVIF и скачивает его"""
    try:
        media_service = cam.media()
        # Запрашиваем URL для получения JPEG-кадра (Snapshot)
        image_uri = media_service.GetSnapshotUri(ProfileToken=profile_token)
        uri = image_uri.Uri
        print(f"Пытаемся скачать скриншот по URL: {uri}")

        # Скачиваем изображение с поддержкой Digest-авторизации Hikvision
        response = requests.get(uri, auth=auth, timeout=5)

        if response.status_code == 200:
            with open(filename, 'wb') as f:
                f.write(response.content)
            print(f"Скриншот успешно сохранен как '{filename}'")
        else:
            print(f"Не удалось скачать кадр. Код ответа: {response.status_code}")

    except Exception as e:
        print(f"Ошибка при создании скриншота: {e}")


def ptz_move(cam, profile_token, x, y, duration=1.0):
    """
    Управление поворотом (PTZ) через ContinuousMove.
    x: Скорость панорамирования (Pan), от -1.0 (влево) до 1.0 (вправо)
    y: Скорость наклона (Tilt), от -1.0 (вниз) до 1.0 (вверх)
    duration: Время движения в секундах
    """
    try:
        ptz_service = cam.ptz()

        # Задаем вектор скорости
        velocity = {
            'PanTilt': {
                'x': x,
                'y': y
            }
        }

        print(f"Запуск движения: влево/вправо={x}, вверх/вниз={y}")
        ptz_service.ContinuousMove(ProfileToken=profile_token, Velocity=velocity)

        # Ждем, пока камера движется
        time.sleep(duration)

        # Обязательно останавливаем движение
        ptz_service.Stop(ProfileToken=profile_token, PanTilt=True, Zoom=True)
        print("Движение остановлено.")

    except Exception as e:
        print(f"Ошибка PTZ-управления: {e}")

def print_menu():
    print("\n" + "="*30)
    print(" Управление Hikvision (ONVIF)")
    print("="*30)
    print(" u - Вверх (Up)")
    print(" d - Вниз (Down)")
    print(" l - Влево (Left)")
    print(" r - Вправо (Right)")
    print(" s - Сделать скриншот")
    print(" q - Выход")
    print("="*30)


if __name__ == "__main__":
    my_cam = create_camera()
    if not my_cam:
        sys.exit()

    media = my_cam.media()
    profiles = media.GetProfiles()
    if not profiles:
        print("Профили не найдены.")
        sys.exit()

    active_token = profiles[0].token
    print(f"Подключено. Используется профиль: {profiles[0].Name}")

    print_menu()

    while True:
        command = input("Введите команду и нажмите Enter: ").strip().lower()

        if command == 'u':
            print("Движение вверх...")
            ptz_move(my_cam, active_token, x=0.0, y=PTZ_SPEED, duration=MOVE_DURATION)
        if command == 'ul':
            print("Движение вверх+влево...")
            ptz_move(my_cam, active_token, x=0.0, y=PTZ_SPEED, duration=MOVE_DURATION)
            ptz_move(my_cam, active_token, x=-PTZ_SPEED, y=0.0, duration=MOVE_DURATION)
        elif command == 'd':
            print("Движение вниз...")
            ptz_move(my_cam, active_token, x=0.0, y=-PTZ_SPEED, duration=MOVE_DURATION)
        elif command == 'l':
            print("Движение влево...")
            ptz_move(my_cam, active_token, x=-PTZ_SPEED, y=0.0, duration=MOVE_DURATION)
        elif command == 'r':
            print("Движение вправо...")
            ptz_move(my_cam, active_token, x=PTZ_SPEED, y=0.0, duration=MOVE_DURATION)
        elif command == 's':
            filename = f"data/snap_{int(time.time())}.jpg"
            print("Запрос скриншота...")
            capture_screenshot(my_cam, active_token, filename=filename)
        elif command == 'q':
            print("Выход из программы.")
            break
        elif command == '':
            continue
        else:
            print("Неизвестная команда. Используйте: u, d, l, r, s, q")
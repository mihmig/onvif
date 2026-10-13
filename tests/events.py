import time
import requests
from onvif import ONVIFCamera
from requests.auth import HTTPDigestAuth
import logging
import sys
import xml.etree.ElementTree as ET

# Настройка детального логирования zeep (ONVIF/SOAP)
logging.basicConfig(level=logging.DEBUG)
logging.getLogger('zeep.client').setLevel(logging.DEBUG)
logging.getLogger('zeep.wsdl').setLevel(logging.DEBUG)
logging.getLogger('zeep.transports').setLevel(logging.DEBUG)

# Дополнительно выведем весь трафик в stdout для удобства копирования
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logging.getLogger('zeep.client').addHandler(handler)
logging.getLogger('zeep.wsdl').addHandler(handler)


# Настройки подключения к камере
IP = "192.168.1.64"  # Замените на IP вашей камеры
PORT = 80  # ONVIF порт Hikvision (обычно совпадает с HTTP-портом)
USER = "onvif1"  # Пользователь, созданный в Integration Protocol
PASS = "Hpt7204Qt"

def create_camera():
    try:
        return ONVIFCamera(IP, PORT, USER, PASS)
    except Exception as e:
        print(f"[Ошибка] Не удалось инициализировать камеру: {e}")
        return None


def capture_screenshot(cam, profile_token):
    """Получает URI скриншота и сохраняет его"""
    try:
        media_service = cam.create_media_service()
        image_uri = media_service.GetSnapshotUri({'ProfileToken': profile_token})
        uri = image_uri.Uri

        filename = f"motion_{int(time.time())}.jpg"
        response = requests.get(uri, auth=HTTPDigestAuth(USER, PASS), timeout=5)

        if response.status_code == 200:
            with open(filename, 'wb') as f:
                f.write(response.content)
            print(f"📷 [СОХРАНЕН] Скриншот детекции движения: '{filename}'")
        else:
            print(f"[Ошибка] Не удалось скачать кадр. Код: {response.status_code}")
    except Exception as e:
        print(f"[Ошибка скриншота]: {e}")


def parse_and_log_event(msg, cam, profile_token):
    """Парсит событие с учетом структуры Hikvision/ONVIF"""
    try:
        # 1. Извлекаем тему из отдельного тега wsnt:Topic
        topic = "Unknown"
        if hasattr(msg, 'Topic') and msg.Topic is not None:
            # У Topic есть свойство '_value_1', которое библиотека иногда игнорирует,
            # поэтому пробуем получить текст напрямую через __dict__ или str()
            try:
                topic = msg.Topic._value_1 or str(msg.Topic)
            except AttributeError:
                topic = str(msg.Topic)

        # 2. Извлекаем данные (State / IsMotion)
        data_value = None

        # Структура: NotificationMessage -> Message -> tt:Message -> Data -> SimpleItem
        if (hasattr(msg, 'Message') and
                msg.Message is not None and
                hasattr(msg.Message, 'Data') and
                msg.Message.Data is not None):

            # Пробуем найти SimpleItem внутри Data
            simple_items = getattr(msg.Message.Data, 'SimpleItem', [])
            if simple_items:
                for item in simple_items:
                    name = getattr(item, 'Name', '')
                    value = getattr(item, 'Value', None)
                    if name in ['State', 'IsMotion']:
                        data_value = str(value).lower() if value else None
                        break

        print(f"🔔 [ЛОГ СОБЫТИЯ] Время: {time.strftime('%X')} | Тип: {topic} | Статус: {data_value}")

        # 3. Проверка на движение
        motion_keywords = ['Motion', 'MotionAlarm', 'CellMotionDetector']
        if any(kw in topic for kw in motion_keywords) and data_value == 'true':
            print("🏃 [ОБНАРУЖЕНО ДВИЖЕНИЕ] Запуск захвата кадра...")
            capture_screenshot(cam, profile_token)

    except Exception as e:
        # Если упало всё — выводим полный дамп объекта для диагностики
        print(f"[Ошибка парсинга]: {e}. Данные сообщения типа: {type(msg)}")



if __name__ == "__main__":
    my_cam = create_camera()
    if not my_cam:
        exit()

    # Получаем профиль для скриншотов
    media = my_cam.create_media_service()
    profiles = media.GetProfiles()
    if not profiles:
        print("[Ошибка] Профили не найдены.")
        exit()
    active_token = profiles[0].token

    try:
        # Инициализируем сервис событий
        event_service = my_cam.create_events_service()

        # Создаем подписку (PullPoint)
        # В параметрах указывается время жизни подписки (например, 10 минут = PT10M)
        pullpoint = event_service.CreatePullPointSubscription({'InitialTerminationTime': 'PT10M'})

        # Создаем клиент для чтения сообщений из созданной точки
        pullpoint_client = my_cam.create_pullpoint_service()

        print(f" Скрипт запущен. Ожидание событий от камеры {IP}...")
        print("В веб-интерфейсе камеры должна быть включена 'Детекция движения' и привязка к ONVIF.")
        print("-" * 80)

        while True:
            # Опрашиваем камеру (Таймаут ожидания сообщения — 5 секунд)
            # За один запрос просим вернуть не более 10 сообщений
            response = pullpoint_client.PullMessages({'Timeout': 'PT5S', 'MessageLimit': 10})

            # Если пришли новые сообщения, обрабатываем их
            if hasattr(response, 'NotificationMessage'):
                for message in response.NotificationMessage:
                    parse_and_log_event(message, my_cam, active_token)

            # Небольшая пауза между циклами опроса, чтобы не перегружать процессор
            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\n Мониторинг остановлен пользователем.")
        # Корректное отключение подписки
        try:
            print("Подписка успешно удалена.")
        except Exception as unsubscribe_error:
            print(f"Не удалось отписаться: {unsubscribe_error}")
    except Exception as e:
        print(f"\n[Критическая ошибка]: {e}")
        print("Проверьте, включена ли детекция движения в настройках самой камеры (Event -> Basic Event -> Motion).")
    finally:
        if pullpoint is not None:
            print("Отписываемся от событий...")
            event_service.Unsubscribe()
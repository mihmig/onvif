import time
import requests
from onvif import ONVIFCamera
from requests.auth import HTTPDigestAuth
import logging
import sys

# Настройка детального логирования zeep (ONVIF/SOAP)
logging.basicConfig(level=logging.DEBUG)
# logging.getLogger('zeep.client').setLevel(logging.DEBUG)
# logging.getLogger('zeep.wsdl').setLevel(logging.DEBUG)
logging.getLogger('zeep.transports').setLevel(logging.DEBUG)
#
# # Дополнительно выведем весь трафик в stdout для удобства копирования
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


def _simple_items_from_message(msg):
    """Читает tt:SimpleItem из XML-элемента в Message._value_1."""
    message = getattr(msg, 'Message', None)
    payload = getattr(message, '_value_1', None)

    if payload is None:
        return []

    # Обычно payload — lxml.etree._Element. В некоторых версиях Zeep
    # xsd:any может быть представлен списком XML-элементов.
    roots = payload if isinstance(payload, (list, tuple)) else [payload]
    items = []

    for root in roots:
        if not hasattr(root, 'iter'):
            continue
        for element in root.iter():
            local_name = element.tag.rsplit('}', 1)[-1]
            if local_name == 'SimpleItem':
                items.append((
                    element.attrib.get('Name'),
                    element.attrib.get('Value'),
                ))

    return items


def parse_and_log_event(msg, cam, profile_token):
    """Парсит событие из сырого XML внутри объекта Zeep."""
    try:
        topic_object = getattr(msg, 'Topic', None)
        topic = getattr(topic_object, '_value_1', None) or 'Unknown'
        simple_items = _simple_items_from_message(msg)

        event_name = None
        data_value = None
        for name, value in simple_items:
            # if name in ('IsMotion', 'State'):
            if name in ('IsMotion'):
                event_name = name
                data_value = value.strip().lower() if value is not None else None
                break

        print(
            f"🔔 [ЛОГ СОБЫТИЯ] Время: {time.strftime('%X')} | "
            f"Тип: {topic} | Поле: {event_name} | Статус: {data_value}"
        )

        topic_is_motion = 'motion' in str(topic).lower()
        is_motion = event_name == 'IsMotion' and data_value == 'true'
        # is_motion = (
        #     event_name == 'IsMotion' and data_value == 'true'
        # ) or (
        #     event_name == 'State' and data_value == 'true' and topic_is_motion
        # )

        if is_motion:
            print("🏃 [ОБНАРУЖЕНО ДВИЖЕНИЕ]")
            # print("🏃 [ОБНАРУЖЕНО ДВИЖЕНИЕ] Запуск захвата кадра...")
            # capture_screenshot(cam, profile_token)

    except Exception as e:
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
    except Exception as e:
        print(f"\n[Критическая ошибка]: {e}")
        print("Проверьте, включена ли детекция движения в настройках самой камеры (Event -> Basic Event -> Motion).")

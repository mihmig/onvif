#!/usr/bin/env python3
"""
Детектор ONVIF-камеры: модель, медиапрофили и доступность снимка.

Показывает:
    0. Модель камеры (производитель, модель, версия ПО, серийный номер).
    1. Доступные профили ONVIF (токен, имя, кодирование, разрешение).
    2. Есть ли URL снимка (snapshot) и отдаёт ли камера по нему картинку.

Запуск:
    python detect.py http://192.168.1.144:8899
    python detect.py http://192.168.1.144:8899 -u admin -p secret
    python detect.py http://192.168.1.144:8899 -u admin -p secret --proxy 192.168.10.252:3128
"""
import argparse
import logging
import os
import sys
from urllib.parse import urlparse

import requests
from onvif import ONVIFClient
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

logger = logging.getLogger(__name__)

# Таймаут сетевых запросов по умолчанию, секунды
DEFAULT_TIMEOUT = 10.0


def parse_args(argv=None) -> argparse.Namespace:
    """Разбирает аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description='Детектор ONVIF-камеры: модель, профили и доступность снимка',
    )
    parser.add_argument('url', help='ссылка на камеру, например http://192.168.1.144:8899')
    parser.add_argument('-u', '--user', help='имя пользователя')
    parser.add_argument('-p', '--password', help='пароль')
    parser.add_argument('--proxy', help='HTTP-прокси без авторизации, например 192.168.10.252:3128')
    parser.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT,
                        help=f'таймаут сети, секунды (по умолчанию {DEFAULT_TIMEOUT:g})')
    parser.add_argument('-v', '--verbose', action='store_true', help='подробный вывод (debug)')
    return parser.parse_args(argv)


def setup_logging(verbose: bool) -> None:
    """По умолчанию глушим шум библиотек, с --verbose включаем debug."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.ERROR,
        format='%(levelname)s: %(message)s',
    )


def parse_camera_url(url: str) -> tuple[str, int, bool]:
    """Разбирает ссылку на камеру -> (host, port, use_https).

    Схему можно не указывать: '192.168.1.144:8899' трактуется как http.
    """
    parsed = urlparse(url if '://' in url else 'http://' + url)
    use_https = (parsed.scheme or 'http').lower() == 'https'
    host = parsed.hostname
    if not host:
        raise ValueError(f'не удалось разобрать ссылку: {url!r}')
    port = parsed.port or (443 if use_https else 80)
    return host, port, use_https


def normalize_proxy(proxy: str | None) -> str | None:
    """Приводит прокси к виду http://host:port."""
    if not proxy:
        return None
    proxy = proxy.strip()
    if not proxy:
        return None
    return proxy if '://' in proxy else 'http://' + proxy


def use_proxy(proxy: str | None) -> dict[str, str] | None:
    """Прописывает прокси в окружение (для SOAP-запросов ONVIF) и возвращает его для requests.

    Библиотека onvif-python ходит в камеру через zeep/requests, которые берут
    прокси из переменных окружения, — поэтому задаём их здесь. Для прямых
    HTTP-запросов (скачивание снимка) прокси передаётся аргументом.
    """
    if not proxy:
        return None
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
        os.environ[name] = proxy
    return {'http': proxy, 'https': proxy}


def print_device_info(device) -> None:
    """Пункт 0: модель камеры и прочие сведения об устройстве."""
    print('0. Модель камеры')
    try:
        info = device.GetDeviceInformation()
    except Exception as error:  # noqa: BLE001 — детектор не должен падать
        print(f'   не удалось получить (GetDeviceInformation): {error}')
        return

    fields = (
        ('Производитель', 'Manufacturer'),
        ('Модель', 'Model'),
        ('Версия ПО', 'FirmwareVersion'),
        ('Серийный номер', 'SerialNumber'),
        ('Аппаратная версия', 'HardwareId'),
    )
    shown = False
    for label, attr in fields:
        value = getattr(info, attr, None)
        if value:
            print(f'   {label}: {value}')
            shown = True
    if not shown:
        print(f'   камера не сообщила данных: {info}')


def describe_profile(profile) -> str:
    """Краткое описание профиля: кодирование, разрешение, частота кадров."""
    parts = []
    encoder = getattr(profile, 'VideoEncoderConfiguration', None)
    if encoder is not None:
        encoding = getattr(encoder, 'Encoding', None)
        if encoding:
            parts.append(str(encoding))
        resolution = getattr(encoder, 'Resolution', None)
        width = getattr(resolution, 'Width', None)
        height = getattr(resolution, 'Height', None)
        if width and height:
            parts.append(f'{width}x{height}')
        rate = getattr(getattr(encoder, 'RateControl', None), 'FrameRateLimit', None)
        if rate:
            parts.append(f'{rate:g} fps')
    return ', '.join(parts)


def get_profiles(media) -> list:
    """Пункт 1: список профилей ONVIF. Возвращает список профилей (может быть пустым)."""
    print('1. Профили ONVIF')
    try:
        profiles = media.GetProfiles()
    except Exception as error:  # noqa: BLE001
        print(f'   не удалось получить (GetProfiles): {error}')
        return []

    if not profiles:
        print('   профили не найдены')
        return []

    for profile in profiles:
        name = getattr(profile, 'Name', '') or ''
        summary = describe_profile(profile)
        tail = f' ({summary})' if summary else ''
        print(f'   - {profile.token}: {name}{tail}')
    return profiles


def auth_from_challenge(response, user: str, password: str):
    """Подбирает Basic/Digest по заголовку WWW-Authenticate камеры."""
    challenge = response.headers.get('WWW-Authenticate', '').lower()
    if 'digest' in challenge:
        return HTTPDigestAuth(user, password)
    return HTTPBasicAuth(user, password)


def check_snapshot(uri: str, user: str, password: str,
                   proxies: dict | None, timeout: float) -> tuple[bool, str]:
    """Проверяет, отдаёт ли камера картинку по URL снимка.

    Сначала запрос без авторизации; если камера просит логин/пароль (401),
    повторяем с Basic или Digest — по её же заголовку WWW-Authenticate.
    """
    try:
        response = requests.get(uri, timeout=timeout, proxies=proxies)
        if response.status_code == 401 and user:
            response = requests.get(uri, auth=auth_from_challenge(response, user, password),
                                    timeout=timeout, proxies=proxies)
        if response.ok:
            content_type = response.headers.get('Content-Type', '?')
            return True, f'{response.status_code}, {content_type}, {len(response.content)} байт'
        return False, f'HTTP {response.status_code}'
    except requests.RequestException as error:
        return False, f'ошибка запроса: {error}'


def print_snapshots(media, profiles: list, user: str, password: str,
                    proxies: dict | None, timeout: float) -> None:
    """Пункт 2: есть ли URL снимка и работает ли он."""
    print('2. URL снимка (snapshot)')
    if not profiles:
        print('   нет профилей — проверять нечего')
        return

    for profile in profiles:
        try:
            uri = media.GetSnapshotUri(ProfileToken=profile.token).Uri
        except Exception as error:  # noqa: BLE001
            print(f'   - {profile.token}: нет ({error})')
            continue

        works, detail = check_snapshot(uri, user, password, proxies, timeout)
        mark = 'да' if works else 'URL есть, но картинка не получена'
        print(f'   - {profile.token}: {mark} — {uri}')
        print(f'     {detail}')


def detect(url: str, user: str | None, password: str | None,
           proxy: str | None, timeout: float) -> int:
    """Подключается к камере и печатает модель, профили и статус снимка."""
    host, port, use_https = parse_camera_url(url)
    proxy_url = normalize_proxy(proxy)
    proxies = use_proxy(proxy_url)

    scheme = 'https' if use_https else 'http'
    print(f'Камера: {host}:{port} ({scheme})')
    if proxy_url:
        print(f'Прокси: {proxy_url}')
    print()

    user = user or ''
    password = password or ''

    try:
        client = ONVIFClient(host, port, user, password,
                             timeout=timeout, use_https=use_https, verify_ssl=False)
        device = client.devicemgmt()
    except Exception as error:  # noqa: BLE001
        print(f'Не удалось подключиться к камере по ONVIF: {error}')
        return 1

    print_device_info(device)
    print()

    try:
        media = client.media()
        profiles = get_profiles(media)
        print()
        print_snapshots(media, profiles, user, password, proxies, timeout)
    except Exception as error:  # noqa: BLE001
        print(f'Не удалось получить медиаданные по ONVIF: {error}')
        return 1

    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    return detect(args.url, args.user, args.password, args.proxy, args.timeout)


if __name__ == '__main__':
    sys.exit(main())

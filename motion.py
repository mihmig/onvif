#!/usr/bin/env python3
"""
Запись роликов по движению: ONVIF-события + RTSP с предзаписью.

    python motion.py RVi-IPC43S-telegram

Все настройки берутся из config/config.yml (камера ищется по `id`).

Как устроено:
  * Поток RTSP читается постоянно (без декодирования, модулем rtsp_recorder)
    и складывается в кольцевой буфер из GOP'ов — групп кадров, каждая из
    которых начинается с ключевого кадра. В буфере держится ровно столько
    GOP'ов, чтобы покрыть `duration_before` секунд, поэтому ролик всегда
    начинается с ключевого кадра не позже, чем за duration_before до события.
  * Главный поток по ONVIF держит PullPoint-подписку на события и сообщает
    записи, есть ли сейчас движение.
  * Началось движение -> в новый MP4 сбрасывается буфер и дальше пишется
    живой поток. Движение кончилось -> пишем ещё `duration_after` секунд и
    закрываем файл на ближайшем ключевом кадре (он же открывает следующий
    буфер, так что соседние ролики стыкуются без потерь).

Подписка ONVIF: камера (RVi-IPC43S) даёт срок до 24 часов, но слотов под
подписки всего несколько, а брошенная подписка занимает слот до истечения
срока. Поэтому берём короткий срок (`subscription_ttl`, 120 с), продлеваем
его через Renew и отписываемся при выходе: упавший процесс освободит слот
сам через пару минут.

Настройки камеры в config.yml (кроме host/port/user/pass):
    duration_before   секунд видео до начала движения (5)
    duration_after    секунд видео после окончания движения (5)
    max_duration      предельная длина одного ролика, дальше — новый файл (300)
    video_dir         каталог для роликов ("data")
    video_prefix      префикс имени файла ("rvi")
    profile_token     ONVIF-профиль потока (по умолчанию первый)
    rtsp_url          RTSP-ссылка вручную, вместо ONVIF GetStreamUri
    motion_topic      топик события ("RuleEngine/CellMotionDetector/Motion")
    subscription_ttl  срок подписки в секундах (120)

Отправка роликов в мессенджер после записи (см. plugins/sender.py): какой
из них выбран, решают токены в конфиге — telegram_bot_token или
max_bot_token, идентификатор чата при этом берётся из telegram_chat_id
или max_chat_id соответственно. Без токена и чата ролики только пишутся.
"""
import collections
import datetime
import logging
import os
import signal
import sys
import threading
import time

import yaml
from onvif import ONVIFClient, ONVIFParser
from onvif.services.events.pullpoint import PullPoint
from onvif.services.events.subscription import Subscription

from plugins.sender import create_sender
from rtsp_recorder import (
    USER_AGENT,
    H264Depacketizer,
    MP4Writer,
    RTSPClient,
    RTSPError,
    parse_rtp,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(threadName)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger(__name__)

CLOCK = 90000  # частота меток времени RTP для H.264
RECONNECT_DELAY = 5
KEEPALIVE_INTERVAL = 30

# Поле с признаком движения: IsMotion у CellMotionDetector, State у MotionAlarm
MOTION_ITEMS = ('IsMotion', 'State')


# --------------------------------------------------------------------------
# Запись: кольцевой буфер + MP4
# --------------------------------------------------------------------------

class MotionRecorder:
    """Читает RTSP в отдельном потоке и пишет ролики, пока есть движение."""

    def __init__(self, url, user, password, config):
        self.url = url
        self.user = user
        self.password = password
        self.config = config
        self.before = float(config.get('duration_before', 5))
        self.after = float(config.get('duration_after', 5))
        self.max_duration = float(config.get('max_duration', 300))
        self.video_dir = config.get('video_dir') or 'data'
        self.prefix = config.get('video_prefix') or 'rvi'

        self.lock = threading.Lock()
        self.motion = False       # движение сейчас
        self.triggered = False    # было начало движения, ролик ещё не открыт
        self.motion_end = 0.0     # time.monotonic() окончания движения
        self.motion_start = None  # datetime начала — для имени файла

        self.gops = collections.deque()  # [[(rtp_ts, ext_ts, nals, key), ...], ...]
        self.writer = None
        self.clip_start = 0.0
        self.clip_frames = 0
        self.clip_first_ts = 0
        self.clip_last_ts = 0

        self.client = None
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name='rtsp', daemon=True)

        # Отправка роликов в мессенджер: Telegram или MAX — по токену в config.yml
        self.sender, self.chat_id = create_sender(config)
        if self.sender:
            logger.info('Отправка роликов: %s, чат %s',
                        type(self.sender).__name__, self.chat_id)

    # ---- вызывается из потока ONVIF ----

    def set_motion(self, is_motion):
        with self.lock:
            if is_motion and not self.motion:
                logger.info('Движение: началось')
                self.triggered = True
                self.motion_start = datetime.datetime.now()
            elif not is_motion and self.motion:
                logger.info('Движение: закончилось')
                self.motion_end = time.monotonic()
            self.motion = is_motion

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopping.set()
        client = self.client
        if client:
            client.close()  # прерывает блокирующее чтение сокета
        self.thread.join(timeout=10)

    # ---- поток RTSP ----

    def _run(self):
        while not self.stopping.is_set():
            try:
                self._stream()
            except (RTSPError, OSError, ValueError) as error:
                if self.stopping.is_set():
                    break
                logger.error('RTSP: %s, переподключение через %s с', error, RECONNECT_DELAY)
            finally:
                self._close_clip()
                self.gops.clear()
            self.stopping.wait(RECONNECT_DELAY)

    def _stream(self):
        self.client = client = RTSPClient(self.url, self.user, self.password, timeout=10)
        try:
            client.connect()
            client.request('OPTIONS')
            track_url, sps, pps = client.describe()
            client.setup_and_play(track_url)
            logger.info('RTSP: поток открыт %s', client.url)

            depacketizer = H264Depacketizer()
            last_raw, ext_ts = None, 0
            keepalive = time.monotonic()

            while not self.stopping.is_set():
                channel, packet = client.read_interleaved()
                if time.monotonic() - keepalive > KEEPALIVE_INTERVAL:
                    self._keepalive(client)
                    keepalive = time.monotonic()
                if channel != 0:  # 1 — RTCP
                    continue
                parsed = parse_rtp(packet)
                if not parsed:
                    continue
                timestamp, _, marker, payload = parsed

                for frame_ts, nals, is_key in depacketizer.push(timestamp, marker, payload):
                    # SPS/PPS могут прийти в самом потоке, а не только в SDP
                    for nal in nals:
                        if nal[0] & 0x1F == 7:
                            sps = nal
                        elif nal[0] & 0x1F == 8:
                            pps = nal
                    # 32-битная метка RTP переполняется — разворачиваем в монотонную
                    if last_raw is not None:
                        delta = (frame_ts - last_raw) & 0xFFFFFFFF
                        ext_ts += delta - (1 << 32) if delta > 0x7FFFFFFF else delta
                    last_raw = frame_ts
                    self._on_frame((frame_ts, ext_ts, nals, is_key), sps, pps)
        finally:
            client.teardown()
            client.close()
            self.client = None

    @staticmethod
    def _keepalive(client):
        """OPTIONS без ожидания ответа, чтобы камера не закрыла сессию по таймауту.

        Ответ придёт посреди RTP-потока, его пропустит read_interleaved.
        """
        client.cseq += 1
        lines = ['OPTIONS {} RTSP/1.0'.format(client.url),
                 'CSeq: {}'.format(client.cseq),
                 'User-Agent: ' + USER_AGENT]
        if client.session:
            lines.append('Session: ' + client.session)
        authorization = client._authorization('OPTIONS', client.url)
        if authorization:
            lines.append('Authorization: ' + authorization)
        client.socket.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode())

    def _on_frame(self, frame, sps, pps):
        _, ext_ts, _, is_key = frame
        now = time.monotonic()

        if self.writer is not None:
            with self.lock:
                self.triggered = False  # новое движение во время записи просто продлевает ролик
                finished = not self.motion and now - self.motion_end >= self.after
            too_long = now - self.clip_start >= self.max_duration
            # Закрываем только на ключевом кадре: он откроет следующий буфер
            if is_key and (finished or too_long):
                self._close_clip()
                if not finished:
                    logger.info('Ролик достиг max_duration, продолжаем в новом файле')
                    with self.lock:
                        self.triggered = True  # движение не кончилось — сразу следующий ролик
                        self.motion_start = datetime.datetime.now()

        if self.writer is not None:
            self._write(frame)
            return

        # Ролик не пишется — копим кольцевой буфер из целых GOP'ов
        if is_key:
            self.gops.append([frame])
        elif self.gops:
            self.gops[-1].append(frame)
        else:
            return  # до первого ключевого кадра кадры бесполезны
        # Выкидываем старый GOP, если следующий и так покрывает duration_before
        while len(self.gops) > 1 and ext_ts - self.gops[1][0][1] >= self.before * CLOCK:
            self.gops.popleft()

        with self.lock:
            start = self.triggered
            self.triggered = False
            started_at = self.motion_start or datetime.datetime.now()
        if start:
            self._open_clip(sps, pps, started_at)

    def _open_clip(self, sps, pps, started_at):
        if not sps or not pps:
            logger.error('Нет SPS/PPS — не из чего собрать заголовок MP4, событие пропущено')
            return
        os.makedirs(self.video_dir, exist_ok=True)
        filename = os.path.join(self.video_dir, '{}_{}.mp4'.format(
            self.prefix, started_at.strftime('%Y%m%d_%H%M%S')))
        if os.path.exists(filename):  # несколько роликов в одну секунду
            filename = filename[:-4] + '_{}.mp4'.format(int(time.time() * 1000) % 1000)
        self.writer = MP4Writer(filename, sps, pps)
        self.clip_start = time.monotonic()
        self.clip_frames = 0
        self.clip_first_ts = self.gops[0][0][1]

        buffered = [frame for gop in self.gops for frame in gop]
        self.gops.clear()
        pre = (buffered[-1][1] - self.clip_first_ts) / CLOCK
        logger.info('Запись в %s (предзапись %.1f с)', filename, pre)
        for frame in buffered:
            self._write(frame)

    def _write(self, frame):
        rtp_ts, ext_ts, nals, is_key = frame
        self.writer.add_sample(nals, rtp_ts, is_key)
        self.clip_frames += 1
        self.clip_last_ts = ext_ts

    def _close_clip(self):
        writer, self.writer = self.writer, None
        if writer is None:
            return
        try:
            if writer.close():
                seconds = (self.clip_last_ts - self.clip_first_ts) / CLOCK
                size_mb = os.path.getsize(writer.path) / (1024 * 1024)
                logger.info('Ролик сохранён: %s (%.1f с, %s кадров, %.2f МБ)',
                            writer.path, seconds, self.clip_frames, size_mb)
                self._notify_messenger(writer.path, seconds)
            else:
                logger.warning('Ролик %s пустой', writer.path)
        except (OSError, ValueError) as error:
            logger.error('Не удалось закрыть ролик %s: %s', writer.path, error)

    def _notify_messenger(self, path: str, seconds: float):
        """Отправляет ролик в мессенджер в отдельном потоке,
        чтобы сетевой запрос не блокировал чтение RTSP."""
        if not self.sender:
            return
        name = self.config.get('name', 'Камера')
        caption = f'{name}: движение, {seconds:.0f} с'
        threading.Thread(
            target=self._send_video,
            args=(path, caption),
            name='send',
            daemon=True,
        ).start()

    def _send_video(self, path: str, caption: str):
        messenger = type(self.sender).__name__
        try:
            self.sender.send_media(
                self.chat_id, caption, path, media_type='video')
            logger.info('%s: ролик отправлен %s',
                        messenger, os.path.basename(path))
        except Exception as error:
            logger.error('%s: не удалось отправить %s: %s',
                         messenger, os.path.basename(path), error)


# --------------------------------------------------------------------------
# ONVIF: подписка на события движения
# --------------------------------------------------------------------------

class MotionEvents:
    """PullPoint-подписка с продлением и пересозданием при ошибках."""

    def __init__(self, config):
        self.config = config
        self.topic = config.get('motion_topic') or 'RuleEngine/CellMotionDetector/Motion'
        self.ttl = int(config.get('subscription_ttl', 120))
        self.parser = ONVIFParser(extract_xpaths={'topic': './/{http://docs.oasis-open.org/wsn/b-2}Topic'})
        self.client = None
        self.subscription = None

    def connect(self):
        c = self.config
        self.client = ONVIFClient(c['host'], c.get('port', 80), c['user'], c['pass'],
                                  plugins=[self.parser])
        return self.client

    def stream_uri(self):
        """RTSP-ссылка выбранного профиля (без логина и пароля)."""
        media = self.client.media()
        token = self.config.get('profile_token')
        tokens = [p.token for p in media.GetProfiles()]
        if token not in tokens:
            if token:
                logger.warning('Профиля %s нет, берём %s', token, tokens[0])
            token = tokens[0]
        return media.GetStreamUri(
            StreamSetup={'Stream': 'RTP-Unicast', 'Transport': {'Protocol': 'RTSP'}},
            ProfileToken=token).Uri

    def run(self, on_motion, stopping):
        while not stopping.is_set():
            try:
                if self.client is None:
                    self.connect()
                self._listen(on_motion, stopping)
            except Exception as error:  # сеть, SOAP Fault, истёкшая подписка — пересоздаём
                if stopping.is_set():
                    break
                logger.error('ONVIF: %s, переподписка через %s с', error, RECONNECT_DELAY)
                self.client = None
            finally:
                self.unsubscribe()
            stopping.wait(RECONNECT_DELAY)

    def _listen(self, on_motion, stopping):
        response = self.client.events().CreatePullPointSubscription(
            InitialTerminationTime='PT{}S'.format(self.ttl))
        address = response.SubscriptionReference.Address._value_1
        # Не client.pullpoint(): onvif-python при перезаписи адреса теряет
        # query-строку (?Idx=N), и камера отвечает SOAP Fault
        common = self.client.common_args
        pullpoint = PullPoint(xaddr=address, **common)
        self.subscription = Subscription(xaddr=address, **common)
        logger.info('ONVIF: подписка %s до %s', address, response.TerminationTime)
        renewed = time.monotonic()

        while not stopping.is_set():
            if time.monotonic() - renewed > self.ttl / 2:
                self.subscription.Renew(TerminationTime='PT{}S'.format(self.ttl))
                renewed = time.monotonic()

            messages = pullpoint.PullMessages(Timeout='PT5S', MessageLimit=100)
            notifications = getattr(messages, 'NotificationMessage', None) or []
            if not isinstance(notifications, list):
                notifications = [notifications]
            # zeep не разбирает Topic (атрибут + текст), его достаёт ONVIFParser
            topics = self.parser.get_extracted_texts('topic', len(notifications))
            for topic, notification in zip(topics, notifications):
                if not topic or not topic.endswith(self.topic):
                    continue
                is_motion = self._motion_value(notification)
                if is_motion is not None:
                    on_motion(is_motion)

    @staticmethod
    def _motion_value(notification):
        element = notification.Message._value_1
        if isinstance(element, list):
            element = element[0] if element else None
        if element is None:
            return None
        for item in element.iter():
            if item.tag.endswith('SimpleItem') and item.get('Name') in MOTION_ITEMS:
                return item.get('Value', '').lower() == 'true'
        return None

    def unsubscribe(self):
        subscription, self.subscription = self.subscription, None
        if subscription is None:
            return
        try:
            subscription.Unsubscribe()
            logger.info('ONVIF: подписка снята')
        except Exception as error:
            logger.warning('ONVIF: не удалось отписаться: %s', error)


# --------------------------------------------------------------------------

def load_camera(camera_id):
    with open('config/config.yml', encoding='utf-8') as file:
        config = yaml.safe_load(file)
    for camera in config['cameras']:
        if camera.get('id') == camera_id:
            return camera
    ids = ', '.join(str(c.get('id')) for c in config['cameras'])
    raise SystemExit('Камера {!r} не найдена в config.yml. Есть: {}'.format(camera_id, ids))


def main():
    if len(sys.argv) < 2:
        raise SystemExit('Использование: python motion.py <id камеры из config.yml>')
    config = load_camera(sys.argv[1])
    logger.info('Камера %s (%s)', config.get('name'), config['host'])

    events = MotionEvents(config)
    stopping = threading.Event()

    # RTSP-ссылку берём из ONVIF, пока камера не ответит — ждём
    url = config.get('rtsp_url')
    while not url:
        try:
            events.connect()
            url = events.stream_uri()
        except Exception as error:
            logger.error('ONVIF недоступен: %s, повтор через %s с', error, RECONNECT_DELAY)
            events.client = None
            time.sleep(RECONNECT_DELAY)

    # SIGTERM (systemd, kill) обрабатываем как Ctrl+C: отписаться и дописать ролик
    def on_sigterm(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_sigterm)

    recorder = MotionRecorder(url, config['user'], config['pass'], config)
    recorder.start()
    try:
        events.run(recorder.set_motion, stopping)
    except KeyboardInterrupt:
        logger.info('Остановка...')
    finally:
        stopping.set()
        events.unsubscribe()
        recorder.stop()  # закрывает недописанный ролик


if __name__ == '__main__':
    main()

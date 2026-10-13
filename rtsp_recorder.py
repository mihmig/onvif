#!/usr/bin/env python3
"""
Запись RTSP-потока (H.264) в MP4 без единой внешней зависимости —
только стандартная библиотека Python 3.

Нужен там, где нельзя поставить ffmpeg или PyAV (роутеры OpenWrt, embedded).
Видео не декодируется: кадры как есть перекладываются из RTP в MP4,
поэтому расход CPU и памяти минимальный (в памяти держится только
таблица сэмплов, сами кадры сразу пишутся в файл).

Ограничения: только H.264, только видео (звук игнорируется),
только RTP over TCP (interleaved) — UDP не нужен и не поддерживается.

CLI:
    python3 rtsp_recorder.py rtsp://192.168.1.64:554/Streaming/Channels/101 10 \
        -u onvif1 -p secret -o clip.mp4
"""
import argparse
import base64
import hashlib
import os
import re
import socket
import struct
import sys
import time

USER_AGENT = 'python-rtsp-recorder/1.0'

# rtsp://[логин[:пароль]@]хост[:порт][/путь][?параметры]
# Свой разбор ссылки вместо urllib.parse: на OpenWrt urllib — отдельный пакет
_URL_RE = re.compile(
    r'^(?P<scheme>rtsps?)://'
    r'(?:(?P<user>[^:@/]*)(?::(?P<password>[^@/]*))?@)?'
    r'(?P<host>\[[^\]]+\]|[^:/?@]+)'
    r'(?::(?P<port>\d+))?'
    r'(?P<tail>[/?].*)?$', re.IGNORECASE)


def _unquote(text):
    """Раскодирует %XX в логине или пароле, взятых из ссылки"""
    return re.sub(r'%([0-9a-fA-F]{2})', lambda m: chr(int(m.group(1), 16)), text)


def split_rtsp_url(url):
    """Возвращает (ссылка без логина и пароля, логин, пароль, хост, порт)"""
    match = _URL_RE.match(url.strip())
    if not match:
        raise ValueError('не похоже на RTSP-ссылку: {}'.format(url))
    host = match.group('host')
    port = int(match.group('port') or 554)
    clean = '{}://{}:{}{}'.format(match.group('scheme').lower(), host, port,
                                  match.group('tail') or '')
    user = _unquote(match.group('user') or '')
    password = _unquote(match.group('password') or '')
    return clean, user, password, host.strip('[]'), port


# --------------------------------------------------------------------------
# Разбор H.264 SPS — нужен, чтобы узнать разрешение кадра для заголовка MP4
# --------------------------------------------------------------------------

class _BitReader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def bit(self):
        byte = self.data[self.pos >> 3]
        value = (byte >> (7 - (self.pos & 7))) & 1
        self.pos += 1
        return value

    def bits(self, count):
        value = 0
        for _ in range(count):
            value = (value << 1) | self.bit()
        return value

    def ue(self):
        """Экспоненциальный код Голомба, беззнаковый"""
        zeros = 0
        while self.bit() == 0:
            zeros += 1
            if zeros > 32:
                raise ValueError('битый SPS')
        return (1 << zeros) - 1 + (self.bits(zeros) if zeros else 0)

    def se(self):
        """Экспоненциальный код Голомба, знаковый"""
        value = self.ue()
        return (value + 1) // 2 if value % 2 else -(value // 2)


def _unescape(data):
    """Убирает emulation prevention bytes (00 00 03 -> 00 00)"""
    out = bytearray()
    i = 0
    while i < len(data):
        if i + 2 < len(data) and data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 3:
            out += b'\x00\x00'
            i += 3
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def sps_resolution(sps):
    """Возвращает (ширина, высота) из SPS или (0, 0), если разобрать не удалось"""
    try:
        reader = _BitReader(_unescape(sps[1:]))  # пропускаем NAL-заголовок
        profile_idc = reader.bits(8)
        reader.bits(16)  # constraint_set_flags + level_idc
        reader.ue()      # seq_parameter_set_id

        chroma_format_idc = 1
        if profile_idc in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
            chroma_format_idc = reader.ue()
            if chroma_format_idc == 3:
                reader.bit()  # separate_colour_plane_flag
            reader.ue()   # bit_depth_luma_minus8
            reader.ue()   # bit_depth_chroma_minus8
            reader.bit()  # qpprime_y_zero_transform_bypass_flag
            if reader.bit():  # seq_scaling_matrix_present_flag
                for i in range(8 if chroma_format_idc != 3 else 12):
                    if reader.bit():
                        last_scale = 8
                        for _ in range(16 if i < 6 else 64):
                            if last_scale != 0:
                                last_scale = (last_scale + reader.se() + 256) % 256
                            if last_scale == 0:
                                break

        reader.ue()  # log2_max_frame_num_minus4
        pic_order_cnt_type = reader.ue()
        if pic_order_cnt_type == 0:
            reader.ue()  # log2_max_pic_order_cnt_lsb_minus4
        elif pic_order_cnt_type == 1:
            reader.bit()
            reader.se()
            reader.se()
            for _ in range(reader.ue()):
                reader.se()

        reader.ue()   # max_num_ref_frames
        reader.bit()  # gaps_in_frame_num_value_allowed_flag
        width_mbs = reader.ue() + 1
        height_map_units = reader.ue() + 1
        frame_mbs_only = reader.bit()
        if not frame_mbs_only:
            reader.bit()  # mb_adaptive_frame_field_flag
        reader.bit()      # direct_8x8_inference_flag

        crop = [0, 0, 0, 0]
        if reader.bit():  # frame_cropping_flag
            crop = [reader.ue() for _ in range(4)]

        sub_w = 2 if chroma_format_idc in (1, 2) else 1
        sub_h = 2 if chroma_format_idc == 1 else 1
        width = width_mbs * 16 - (crop[0] + crop[1]) * sub_w
        height = ((2 - frame_mbs_only) * height_map_units * 16
                  - (crop[2] + crop[3]) * sub_h * (2 - frame_mbs_only))
        return width, height
    except Exception:
        return 0, 0


# --------------------------------------------------------------------------
# Минимальный мультиплексор MP4 (видеодорожка H.264)
# --------------------------------------------------------------------------

def _box(kind, *payload):
    body = b''.join(payload)
    return struct.pack('>I', 8 + len(body)) + kind + body


class MP4Writer:
    """Пишет кадры сразу в файл, в памяти держит только таблицу сэмплов"""

    def __init__(self, path, sps, pps, timescale=90000):
        self.path = path
        self.sps = sps
        self.pps = pps
        self.timescale = timescale
        self.width, self.height = sps_resolution(sps)
        self.samples = []  # (размер, метка времени, ключевой ли кадр)
        self.file = open(path, 'wb')
        self.file.write(_box(b'ftyp', b'isom', struct.pack('>I', 0x200),
                             b'isom' + b'iso2' + b'avc1' + b'mp41'))
        self.mdat_pos = self.file.tell()
        self.file.write(struct.pack('>I', 0) + b'mdat')  # размер допишем в конце
        self.data_start = self.file.tell()

    def add_sample(self, nals, timestamp, is_keyframe):
        # В MP4 кадры хранятся в формате AVCC: перед каждым NAL идёт его длина
        payload = b''.join(struct.pack('>I', len(nal)) + nal for nal in nals)
        self.file.write(payload)
        self.samples.append((len(payload), timestamp, is_keyframe))

    def _durations(self):
        durations = []
        for i in range(len(self.samples) - 1):
            # Метка времени RTP 32-битная и может переполниться
            delta = (self.samples[i + 1][1] - self.samples[i][1]) & 0xFFFFFFFF
            if delta == 0 or delta > 0x7FFFFFFF:
                delta = durations[-1] if durations else self.timescale // 25
            durations.append(delta)
        durations.append(durations[-1] if durations else self.timescale // 25)
        return durations

    def _moov(self, durations):
        total = sum(durations)
        movie_timescale = 1000
        movie_duration = total * movie_timescale // self.timescale

        # stts: длительности кадров, упакованные группами
        entries = []
        for duration in durations:
            if entries and entries[-1][1] == duration:
                entries[-1][0] += 1
            else:
                entries.append([1, duration])
        stts = _box(b'stts', struct.pack('>II', 0, len(entries)),
                    b''.join(struct.pack('>II', count, value) for count, value in entries))

        keyframes = [i + 1 for i, sample in enumerate(self.samples) if sample[2]]
        stss = _box(b'stss', struct.pack('>II', 0, len(keyframes)),
                    b''.join(struct.pack('>I', number) for number in keyframes))
        # Все кадры лежат в файле подряд, поэтому это один чанк
        stsc = _box(b'stsc', struct.pack('>II', 0, 1),
                    struct.pack('>III', 1, len(self.samples), 1))
        stsz = _box(b'stsz', struct.pack('>III', 0, 0, len(self.samples)),
                    b''.join(struct.pack('>I', sample[0]) for sample in self.samples))
        stco = _box(b'stco', struct.pack('>II', 0, 1), struct.pack('>I', self.data_start))

        avcc = _box(b'avcC',
                    bytes([1, self.sps[1], self.sps[2], self.sps[3], 0xFF, 0xE1]),
                    struct.pack('>H', len(self.sps)), self.sps,
                    b'\x01', struct.pack('>H', len(self.pps)), self.pps)
        avc1 = _box(b'avc1',
                    b'\x00' * 6, struct.pack('>H', 1),      # reserved + data_reference_index
                    b'\x00' * 16,                           # pre_defined + reserved
                    struct.pack('>HH', self.width, self.height),
                    struct.pack('>II', 0x00480000, 0x00480000),  # 72 dpi
                    b'\x00' * 4, struct.pack('>H', 1),      # reserved + frame_count
                    b'\x00' * 32,                           # compressorname
                    struct.pack('>H', 0x0018), b'\xff\xff',  # depth + pre_defined
                    avcc)
        stsd = _box(b'stsd', struct.pack('>II', 0, 1), avc1)
        stbl = _box(b'stbl', stsd, stts, stss, stsc, stsz, stco)

        dref = _box(b'dref', struct.pack('>II', 0, 1), _box(b'url ', struct.pack('>I', 1)))
        minf = _box(b'minf',
                    _box(b'vmhd', struct.pack('>I', 1), b'\x00' * 8),
                    _box(b'dinf', dref),
                    stbl)
        mdhd = _box(b'mdhd', struct.pack('>IIIIIHH', 0, 0, 0, self.timescale, total, 0x55C4, 0))
        hdlr = _box(b'hdlr', struct.pack('>II', 0, 0), b'vide', b'\x00' * 12, b'VideoHandler\x00')
        mdia = _box(b'mdia', mdhd, hdlr, minf)

        matrix = struct.pack('>9I', 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
        tkhd = _box(b'tkhd',
                    struct.pack('>I', 7),  # версия 0, флаги: дорожка включена
                    struct.pack('>IIIII', 0, 0, 1, 0, movie_duration),
                    b'\x00' * 8, struct.pack('>hhhh', 0, 0, 0, 0), matrix,
                    struct.pack('>II', self.width << 16, self.height << 16))
        trak = _box(b'trak', tkhd, mdia)
        mvhd = _box(b'mvhd',
                    struct.pack('>IIIII', 0, 0, 0, movie_timescale, movie_duration),
                    struct.pack('>IHH', 0x00010000, 0x0100, 0), b'\x00' * 8, matrix,
                    b'\x00' * 24, struct.pack('>I', 2))
        return _box(b'moov', mvhd, trak)

    def close(self):
        if not self.samples:
            self.file.close()
            return False
        mdat_size = self.file.tell() - self.mdat_pos
        if mdat_size > 0xFFFFFFFF:
            self.file.close()
            raise ValueError('ролик больше 4 ГБ не поддерживается')
        self.file.write(self._moov(self._durations()))
        self.file.seek(self.mdat_pos)
        self.file.write(struct.pack('>I', mdat_size))
        self.file.close()
        return True


class H264Writer:
    """Запасной формат: сырой поток Annex-B (.h264), вообще без контейнера"""

    def __init__(self, path, sps, pps, timescale=90000):
        self.path = path
        self.count = 0
        self.file = open(path, 'wb')
        self.file.write(b'\x00\x00\x00\x01' + sps + b'\x00\x00\x00\x01' + pps)

    def add_sample(self, nals, timestamp, is_keyframe):
        for nal in nals:
            self.file.write(b'\x00\x00\x00\x01' + nal)
        self.count += 1

    def close(self):
        self.file.close()
        return self.count > 0


# --------------------------------------------------------------------------
# RTSP-клиент
# --------------------------------------------------------------------------

class RTSPError(Exception):
    pass


class RTSPClient:
    def __init__(self, url, user=None, password=None, timeout=10):
        # URL без логина и пароля — именно он участвует в подписи Digest
        self.url, url_user, url_password, self.host, self.port = split_rtsp_url(url)
        self.user = user if user is not None else url_user
        self.password = password if password is not None else url_password
        self.timeout = timeout
        self.cseq = 0
        self.session = None
        self.auth = None  # ('basic', {}) или ('digest', {параметры})
        self.socket = None
        self.buffer = b''

    # ---- транспорт ----

    def connect(self):
        self.socket = socket.create_connection((self.host, self.port), self.timeout)
        self.socket.settimeout(self.timeout)

    def close(self):
        if self.socket:
            try:
                self.socket.close()
            finally:
                self.socket = None

    def _fill(self, size):
        while len(self.buffer) < size:
            chunk = self.socket.recv(65536)
            if not chunk:
                raise RTSPError('соединение закрыто камерой')
            self.buffer += chunk

    def _read(self, size):
        self._fill(size)
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def _read_line(self):
        while b'\r\n' not in self.buffer:
            chunk = self.socket.recv(65536)
            if not chunk:
                raise RTSPError('соединение закрыто камерой')
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b'\r\n', 1)
        return line.decode('utf-8', 'replace')

    # ---- авторизация ----

    @staticmethod
    def _md5(text):
        return hashlib.md5(text.encode()).hexdigest()

    def _authorization(self, method, uri):
        if not self.auth or not self.user:
            return None
        kind, params = self.auth
        if kind == 'basic':
            token = base64.b64encode('{}:{}'.format(self.user, self.password).encode()).decode()
            return 'Basic ' + token

        realm = params.get('realm', '')
        nonce = params.get('nonce', '')
        ha1 = self._md5('{}:{}:{}'.format(self.user, realm, self.password))
        ha2 = self._md5('{}:{}'.format(method, uri))
        header = ('Digest username="{}", realm="{}", nonce="{}", uri="{}"'
                  .format(self.user, realm, nonce, uri))

        qop = params.get('qop')
        if qop and 'auth' in qop:
            cnonce = os.urandom(8).hex()
            nc = '00000001'
            response = self._md5('{}:{}:{}:{}:auth:{}'.format(ha1, nonce, nc, cnonce, ha2))
            header += ', qop=auth, nc={}, cnonce="{}"'.format(nc, cnonce)
        else:
            response = self._md5('{}:{}:{}'.format(ha1, nonce, ha2))
        header += ', response="{}"'.format(response)

        if 'opaque' in params:
            header += ', opaque="{}"'.format(params['opaque'])
        return header

    def _parse_challenge(self, header):
        if header.lower().startswith('basic'):
            self.auth = ('basic', {})
        else:
            params = dict(re.findall(r'(\w+)="([^"]*)"', header))
            match = re.search(r'qop=([\w,\s-]+)', header)
            if match and 'qop' not in params:
                params['qop'] = match.group(1).strip()
            self.auth = ('digest', params)

    # ---- запросы ----

    def request(self, method, uri=None, headers=None, retry=True):
        uri = uri or self.url
        self.cseq += 1
        lines = ['{} {} RTSP/1.0'.format(method, uri),
                 'CSeq: {}'.format(self.cseq),
                 'User-Agent: ' + USER_AGENT]
        if self.session:
            lines.append('Session: ' + self.session)
        authorization = self._authorization(method, uri)
        if authorization:
            lines.append('Authorization: ' + authorization)
        for key, value in (headers or {}).items():
            lines.append('{}: {}'.format(key, value))
        self.socket.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode())

        status, response_headers, body = self._read_response()
        if status == 401 and retry and self.user:
            challenge = response_headers.get('www-authenticate')
            if challenge:
                self._parse_challenge(challenge)
                return self.request(method, uri, headers, retry=False)
        if status != 200:
            raise RTSPError('{} -> {}'.format(method, status))
        return response_headers, body

    def _read_response(self):
        line = self._read_line()
        while not line.startswith('RTSP/'):  # пропускаем мусор до начала ответа
            line = self._read_line()
        status = int(line.split()[1])
        headers = {}
        while True:
            line = self._read_line()
            if not line:
                break
            if ':' in line:
                key, value = line.split(':', 1)
                headers[key.strip().lower()] = value.strip()
        length = int(headers.get('content-length', 0))
        body = self._read(length).decode('utf-8', 'replace') if length else ''
        if 'session' in headers:
            self.session = headers['session'].split(';')[0].strip()
        return status, headers, body

    # ---- SDP ----

    def describe(self):
        """Возвращает (URL видеодорожки, SPS, PPS)"""
        headers, sdp = self.request('DESCRIBE', headers={'Accept': 'application/sdp'})
        base = headers.get('content-base') or headers.get('content-location') or self.url

        control, sps, pps = None, None, None
        in_video = False
        for line in sdp.splitlines():
            line = line.strip()
            if line.startswith('m='):
                in_video = line.startswith('m=video')
            elif in_video and line.startswith('a=control:'):
                control = line[len('a=control:'):].strip()
            elif in_video and 'sprop-parameter-sets=' in line:
                sets = line.split('sprop-parameter-sets=')[1].split(';')[0].strip()
                parts = sets.split(',')
                if len(parts) >= 2 and parts[0] and parts[1]:
                    sps = base64.b64decode(parts[0] + '==')
                    pps = base64.b64decode(parts[1] + '==')

        if control in (None, '*'):
            track_url = base
        elif control.startswith('rtsp://'):
            track_url = control
        else:
            track_url = base.rstrip('/') + '/' + control.lstrip('/')
        return track_url, sps, pps

    def setup_and_play(self, track_url):
        self.request('SETUP', track_url,
                     {'Transport': 'RTP/AVP/TCP;unicast;interleaved=0-1'})
        self.request('PLAY', self.url, {'Range': 'npt=0.000-'})

    def teardown(self):
        try:
            if self.socket and self.session:
                self.request('TEARDOWN', self.url)
        except Exception:
            pass

    def read_interleaved(self):
        """Возвращает (номер канала, данные) очередного чередующегося пакета"""
        while True:
            marker = self._read(1)
            if marker == b'$':
                channel, length = struct.unpack('>BH', self._read(3))
                return channel, self._read(length)
            if marker == b'R':
                # Внеочередной RTSP-ответ посреди потока — дочитываем и игнорируем
                self.buffer = b'R' + self.buffer
                self._read_response()


# --------------------------------------------------------------------------
# Разбор RTP и сборка кадров (RFC 3550, RFC 6184)
# --------------------------------------------------------------------------

def parse_rtp(packet):
    """Возвращает (метка времени, номер пакета, marker, полезная нагрузка) или None"""
    if len(packet) < 12:
        return None
    flags, marker_pt, sequence, timestamp = struct.unpack('>BBHI', packet[:8])
    has_padding = (flags >> 5) & 1
    has_extension = (flags >> 4) & 1
    csrc_count = flags & 0x0F
    offset = 12 + csrc_count * 4
    if has_extension:
        if len(packet) < offset + 4:
            return None
        words = struct.unpack('>H', packet[offset + 2:offset + 4])[0]
        offset += 4 + words * 4
    payload = packet[offset:]
    if has_padding and payload:
        payload = payload[:-payload[-1]]
    if not payload:
        return None
    return timestamp, sequence, bool(marker_pt >> 7), payload


class H264Depacketizer:
    """Собирает NAL-юниты из RTP-пакетов в целые кадры (access units)"""

    def __init__(self):
        self.nals = []
        self.fragment = None
        self.timestamp = None

    def push(self, timestamp, marker, payload):
        """Возвращает список готовых кадров: [(метка времени, [NAL...], ключевой)]"""
        frames = []
        if self.timestamp is not None and timestamp != self.timestamp and self.nals:
            frames.append(self._flush())
        self.timestamp = timestamp

        nal_type = payload[0] & 0x1F
        if 1 <= nal_type <= 23:          # одиночный NAL
            self.nals.append(payload)
        elif nal_type == 24:             # STAP-A: несколько NAL в одном пакете
            offset = 1
            while offset + 2 <= len(payload):
                size = struct.unpack('>H', payload[offset:offset + 2])[0]
                offset += 2
                if size and offset + size <= len(payload):
                    self.nals.append(payload[offset:offset + size])
                offset += size
        elif nal_type == 28:             # FU-A: NAL, разбитый на несколько пакетов
            if len(payload) < 2:
                return frames
            start = payload[1] >> 7
            end = (payload[1] >> 6) & 1
            if start:
                header = bytes([(payload[0] & 0xE0) | (payload[1] & 0x1F)])
                self.fragment = bytearray(header + payload[2:])
            elif self.fragment is not None:
                self.fragment += payload[2:]
            if end and self.fragment is not None:
                self.nals.append(bytes(self.fragment))
                self.fragment = None

        if marker and self.nals:
            frames.append(self._flush())
        return frames

    def _flush(self):
        nals, self.nals = self.nals, []
        is_keyframe = any((nal[0] & 0x1F) == 5 for nal in nals)
        return self.timestamp, nals, is_keyframe


# --------------------------------------------------------------------------
# Основная функция
# --------------------------------------------------------------------------

def record_rtsp(url, duration, filename=None, user=None, password=None,
                timeout=10, verbose=True):
    """
    Записывает `duration` секунд RTSP-потока в файл.

    url:       rtsp://... (логин и пароль можно указать прямо в ссылке)
    duration:  длительность в секундах
    filename:  имя файла; расширение .h264 даёт сырой поток вместо MP4
    user/pass: учётные данные, если их нет в ссылке

    Возвращает путь к файлу или None при ошибке.
    """
    if filename is None:
        filename = 'rec_{}.mp4'.format(int(time.time()))
    directory = os.path.dirname(filename)
    if directory:
        os.makedirs(directory, exist_ok=True)

    def log(message):
        if verbose:
            print(message)

    client = RTSPClient(url, user, password, timeout)
    writer_class = H264Writer if filename.lower().endswith(('.h264', '.264')) else MP4Writer
    writer = None
    frames = 0
    started = time.monotonic()

    try:
        client.connect()
        client.request('OPTIONS')
        track_url, sps, pps = client.describe()
        if not sps or not pps:
            raise RTSPError('в SDP нет параметров H.264 (sprop-parameter-sets)')
        client.setup_and_play(track_url)
        log('Запись {} сек. в {!r}...'.format(duration, filename))

        depacketizer = H264Depacketizer()
        first_timestamp = None
        done = False

        while not done:
            channel, packet = client.read_interleaved()
            if channel != 0:  # 1 — это RTCP, он нам не нужен
                continue
            parsed = parse_rtp(packet)
            if not parsed:
                continue
            timestamp, _, marker, payload = parsed

            for frame_ts, nals, is_keyframe in depacketizer.push(timestamp, marker, payload):
                if writer is None:
                    # Начинаем с ключевого кадра, иначе начало ролика рассыпется
                    if not is_keyframe:
                        continue
                    writer = writer_class(filename, sps, pps)
                    first_timestamp = frame_ts
                if ((frame_ts - first_timestamp) & 0xFFFFFFFF) / 90000.0 >= duration:
                    done = True
                    break
                writer.add_sample(nals, frame_ts, is_keyframe)
                frames += 1

            # Страховка на случай некорректных меток времени в потоке
            if not done and time.monotonic() - started > duration + 30:
                log('Превышено время ожидания потока, запись прервана.')
                break
    except (RTSPError, OSError, ValueError) as error:
        log('Ошибка записи потока: {}'.format(error))
        if '401' in str(error) and 'profile=' not in url:
            log('Похоже на ONVIF-пользователя (раздел Integration Protocol): такому пользователю'
                ' камера отдаёт поток только по ссылке с параметром профиля, например'
                ' .../Streaming/Channels/102?profile=Profile_2 — именно её возвращает'
                ' ONVIF-метод GetStreamUri.')
        if writer:
            writer.close()
        return None
    finally:
        client.teardown()
        client.close()

    if writer is None or not writer.close():
        log('Не получено ни одного кадра, файл не создан.')
        return None

    size_mb = os.path.getsize(filename) / (1024 * 1024)
    log('Видео успешно сохранено: {!r} ({} кадров, {:.2f} МБ)'.format(filename, frames, size_mb))
    return filename


def main():
    parser = argparse.ArgumentParser(description='Запись RTSP-потока в MP4 без ffmpeg')
    parser.add_argument('url', help='RTSP-ссылка')
    parser.add_argument('duration', type=float, help='длительность в секундах')
    parser.add_argument('-o', '--output', help='имя файла (.mp4 или .h264)')
    parser.add_argument('-u', '--user', help='логин')
    parser.add_argument('-p', '--password', help='пароль')
    parser.add_argument('--timeout', type=float, default=10, help='таймаут сети, сек')
    args = parser.parse_args()

    result = record_rtsp(args.url, args.duration, args.output, args.user, args.password,
                         timeout=args.timeout)
    return 0 if result else 1


if __name__ == '__main__':
    sys.exit(main())

### Китайская поворотная камера 
на базе чипа IPC_GK7201V200_G3-WQ-G_S38
 
Codec: MPEG-H Part2/HEVC (H.265) (hevc)
Video resolution: 2304x1296
Frame rate: 12
rtsp://192.168.1.144:554/user=USER_password=PASSWORD_channel=0_stream=0&onvif=0.sdp?real_stream

Codec: MPEG-H Part2/HEVC (H.265) (hevc)
Video resolution: 800x448
Frame rate: 12
rtsp://192.168.1.144:554/user=USER_password=PASSWORD_channel=0_stream=1.sdp?real_stream

Ошибка [Errno 1] Operation not permitted в PyAV/FFmpeg для RTSP обычно возникает в двух ситуациях:

Истёк таймаут ожидания ответа камеры при сетевом рукопожатии (DESCRIBE/SETUP/PLAY) — 10 секунд для медленного ответа Xiongmai не хватало.
Камера не успела освободить предыдущую сессию: у китайских плат Xiongmai/XM процессор слабый и держит 1–2 сессии RTSP. Если предыдущее соединение закрылось только что, следующий запрос мгновенно получает отказ (Operation not permitted), а через 1–2 секунды (в VLC) уже подключается нормально («через раз»).
Что сделано в 
xiongmai.py
:
Поднят таймаут сокета и открытия потока:
Таймаут по умолчанию поднят с 10 до 25 секунд (default_stream_timeout = 25.0).
Передаётся сокетный таймаут FFmpeg в микросекундах: 'stimeout': str(int(timeout * 1_000_000)).
Добавлен retry loop с паузой:
get_image
 и 
_record_stream
 теперь делают до 3 попыток (default_stream_attempts = 3).
Между попытками выдерживается пауза 1.5 секунды (default_stream_retry_delay = 1.5), чтобы камера успела закрыть предыдущую сессию и освободить RTSP-демуксер.
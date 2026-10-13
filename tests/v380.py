import subprocess
import time
from pathlib import Path

rtsp_url = "rtsp://192.168.1.166/live/ch00_0"

filename = Path("../data") / f"camera_{time.strftime('%Y-%m-%d-%H-%M-%S')}.jpg"
filename.parent.mkdir(exist_ok=True)

subprocess.run([
    "ffmpeg",
    "-y",
    "-loglevel", "error",
    "-rtsp_transport", "tcp",
    "-i", rtsp_url,
    "-frames:v", "1",
    "-q:v", "2",
    str(filename),
], check=True)

print(f"Снимок сохранён: {filename}")
"""Local pipeline verification: capture -> pacer -> server, fetch /stream.wav,
check header matches probed format and byte rate ~= nominal over 12 s."""
import struct
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from streamer.capture import LoopbackCapture
from streamer.pacer import Pacer
from streamer.server import StreamServer

capture = LoopbackCapture(on_data=lambda d: pacer.feed(d))
server = StreamServer(capture.format, 8765)
pacer = Pacer(capture.format, sink=server.feed)

server.start()
pacer.start()
capture.start()
time.sleep(1)

req = urllib.request.Request("http://127.0.0.1:8765/stream.wav")
resp = urllib.request.urlopen(req, timeout=10)

header = resp.read(44)
assert header[:4] == b"RIFF" and header[8:12] == b"WAVE", "bad RIFF header"
riff_size = struct.unpack("<I", header[4:8])[0]
n_channels, rate = struct.unpack("<HI", header[22:28])
bits = struct.unpack("<H", header[34:36])[0]
data_size = struct.unpack("<I", header[40:44])[0]
print(f"header: rate={rate} channels={n_channels} bits={bits} "
      f"riff_size={riff_size:#x} data_size={data_size:#x}")
assert rate == capture.format.rate, "header rate != probed rate"
assert n_channels == capture.format.channels
assert bits == 16
assert riff_size == 0xFFFFFFFF and data_size == 0xFFFFFFFF

SECONDS = 12
got = 0
deadline = time.time() + SECONDS
while time.time() < deadline:
    chunk = resp.read(4096)
    if not chunk:
        break
    got += len(chunk)

nominal = capture.format.bytes_per_second * SECONDS
ratio = got / nominal
print(f"received {got} bytes in {SECONDS}s; nominal {nominal}; ratio {ratio:.3f}")
print(f"pacer: real={pacer.real_bytes_sent} silence={pacer.silence_bytes_sent} "
      f"dropped={pacer.dropped_bytes}")
assert 0.97 <= ratio <= 1.03, "byte rate off nominal by >3% - clock problem"

resp.close()
capture.stop(); pacer.stop(); server.stop()
print("PIPELINE VERIFICATION PASSED")

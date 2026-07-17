# experiments/

Dev-only tooling. Nothing here ships in the app or the exe.

- `mirroring/` - hardware probe harnesses for the Cast mirroring path
  (`streamer/mirror.py`). They import the shipped code and talk to real
  speakers:
  - `probe_answer.py` - media-free OFFER/ANSWER probe (does a device accept a
    mirroring session?).
  - `stream_probe.py` - streams a synthetic Opus source and reports transport
    health (checkpoint advance, NACKs, playout delay).
  - `mirror_live.py` - casts real desktop audio via `MirrorSession` for the
    audible + glass-to-glass latency check.
  - `opus_source.py` - synthetic Opus frames for the probes (needs PyAV, listed
    in `mirroring/requirements.txt`; the shipped app encodes via ctypes instead).

Run from the repo root, e.g. `.venv\Scripts\python -m experiments.mirroring.probe_answer "Living Room"`.

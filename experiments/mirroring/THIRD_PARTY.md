# Third-party code (mirror spike)

The Cast mirroring protocol logic proven by this spike now lives in
`streamer/mirror.py`; its third-party attribution (chromecast-sink MIT,
openscreen BSD reference, libopus BSD-3) is recorded in the shipped manifest
at [assets/README.md](../../assets/README.md).

What remains here is spike-only tooling that never ships:
- `opus_source.py` - synthetic PyAV Opus source for the probes (PyAV is a
  dev-only dependency; see requirements.txt).
- `probe_answer.py`, `stream_probe.py` - hardware probes that import the
  protocol from `streamer.mirror`.

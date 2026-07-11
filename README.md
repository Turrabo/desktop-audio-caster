# Desktop Audio Streamer

Casts whole-device Windows audio to Google Nest / Chromecast speakers and speaker groups.
Built to replace SamDel's ChromeCast-Desktop-Audio-Streamer after it proved unreliable
(flaky connects, latency drifting to 10 s). Design + rationale: [docs/plan.md](docs/plan.md).

Reliability model: battle-tested [pychromecast](https://github.com/home-assistant-libs/pychromecast)
for discovery/control, lossless PCM WAV over HTTP on the LAN (no encoder), one
single-clock pacer so latency is fixed and can never drift, a resident discovery
browser plus one watchdog for recovery. Latency is the Cast receiver's own buffer
(~2-3 s, fixed) — see the plan for why sub-second needs a different protocol.

## Use

```
.venv\Scripts\python cli.py devices                  # list speakers/groups
.venv\Scripts\python cli.py start "Living Room"      # cast (Ctrl+C stops)
.venv\Scripts\python -m streamer.tray                # tray app
```

Config + logs: `%APPDATA%\desktop-audio-streamer\`.

## Silent-machine behaviour

While casting, the local output endpoint is muted (loopback capture on this
machine survives mute — verified empirically; it does NOT survive volume-0, so
keep local volume above zero and let the app mute). A marker file restores the
endpoint if the app dies without cleanup.

## Volume architecture

All speaker-volume writes go through one choke point, `streamer/safety.py`, whose rules
are config-driven (`max_volume`, `office_names`, `allow_group_volume`). Shipped defaults
are fully permissive — no cap, no protected devices, group volume allowed.
`tests/test_no_rogue_volume.py` fails if any other module gains a volume call, and the
test suite exercises the mechanism with restrictive rules.

## Tests

```
.venv\Scripts\python -m unittest discover -s tests
```

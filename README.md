# Desktop Audio Streamer

Casts whole-device Windows audio to Google Nest / Chromecast speakers and speaker groups.
Built to replace SamDel's ChromeCast-Desktop-Audio-Streamer after it proved unreliable
(flaky connects, latency drifting to 10 s). Design + rationale: [docs/plan.md](docs/plan.md).

Reliability model: battle-tested [pychromecast](https://github.com/home-assistant-libs/pychromecast)
for discovery/control, lossless PCM WAV over HTTP on the LAN (no encoder), one
single-clock pacer so latency is fixed and can never drift, a resident discovery
browser plus one watchdog for recovery. The receiver pre-buffers ~9 s of live WAV;
the watchdog auto-trims that by seeking to the live edge, landing at ~1.1 s end-to-end
(~1.3 s for groups), measured. It re-trims automatically if lag ever creeps past 2 s.
True sub-second would need the Cast mirroring protocol — different project.

## Use

```
.venv\Scripts\python cli.py devices                  # list speakers/groups
.venv\Scripts\python cli.py start "Living Room"      # cast (Ctrl+C stops)
.venv\Scripts\pythonw.exe launch_tray.pyw            # tray app (windowless)
```

Tray: left-click opens a Material 3 dark popover (GROUPS / SPEAKERS) with cast
toggles, per-device volume sliders, live status (grey idle / amber working /
blue casting / red error), a start-with-Windows switch and Exit. The popover
scales to the monitor's DPI and uses native Win11 rounded corners + shadow.
Launching a second instance just pops the first one's popover.
UI design: [docs/ui-plan.md](docs/ui-plan.md).

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

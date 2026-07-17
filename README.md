# Desktop Audio Streamer

Casts whole-device Windows audio to Google Nest / Chromecast speakers and
speaker groups, from a tray icon.

Built to replace SamDel's ChromeCast-Desktop-Audio-Streamer after it proved
unreliable (flaky connects, latency drifting to 10 s). Design + rationale:
[docs/plan.md](docs/plan.md).

## Install

1. Download **DesktopAudioStreamer.exe** from the
   [latest release](https://github.com/Turrabo/desktop-audio-caster/releases/latest).
2. Run it. A cast icon appears in the system tray (near the clock). No Python
   or setup needed.

Windows SmartScreen may warn on first run because the exe is unsigned — choose
**More info → Run anyway**. Casting needs the speakers on the same LAN.

On first launch the app asks for administrator approval once (a Windows
`netsh` prompt) to register its firewall rule. Speakers fetch the audio over
the network, and Windows scopes its own firewall prompt to whichever network
profile happens to be active — flip between Private and Public later and
casting silently breaks. The app's rule covers every profile but only accepts
connections from your local subnet. If you decline the approval, allow the
app for **both** private and public networks when Windows asks on the first
cast; otherwise casting stops with a firewall error explaining the fix.

## Use

Left-click the tray icon for a popover listing your speaker **Groups** and
**Speakers**. Click a device's play button to start casting; the button spins
while it connects, then becomes a stop button. Each device has a volume slider
(mouse-wheel over it nudges ±2%). The header shows live status — grey idle,
amber connecting, blue casting, red error. Toggle **Start with Windows** and
**Exit** at the bottom. Launching the app again just re-opens the popover.

Config + logs live in `%APPDATA%\desktop-audio-streamer\`.

## How it works

Two cast paths, chosen automatically per speaker (`cast_mode` in config:
`auto` by default, or force `mirror` / `http`):

- **Mirror (default when possible)** — the same low-latency Cast protocol
  Chrome uses for tab casting: Opus audio over encrypted RTP, negotiated
  directly with the speaker. Sub-second end-to-end (~0.4 s receiver playout
  delay, measured), solo and group. Needs 48 kHz stereo capture (the usual
  Windows default); anything else uses the HTTP path.
- **HTTP (automatic fallback)** — battle-tested
  [pychromecast](https://github.com/home-assistant-libs/pychromecast) plus
  lossless PCM WAV over the LAN to the Default Media Receiver. The receiver
  pre-buffers ~9 s of live WAV; one watchdog auto-trims that by seeking to the
  live edge, landing at ~1.1 s end-to-end (~1.3 s for groups), measured, and
  re-trims if lag creeps past 2 s.

Both share one single-clock pacer (fixed, drift-free latency), a resident
discovery browser, and a watchdog. If a mirror session can't start or drops,
the app falls back to the HTTP path automatically, so casting keeps working
even though mirroring rides an undocumented protocol Google could change; set
`cast_mode` to `http` to pin the stable path. Audio via a bundled
[libopus](https://opus-codec.org/); encryption via Windows' own crypto (no
extra dependencies). See [docs/mirror-plan.md](docs/mirror-plan.md).

## Silent-machine behaviour

While casting, the local output endpoint is muted (loopback capture on this
machine survives mute — verified empirically; it does NOT survive volume-0, so
keep local volume above zero and let the app mute). A marker file restores the
endpoint if the app dies without cleanup.

## Volume safety

All speaker-volume writes go through one choke point, `streamer/safety.py`, whose
rules are config-driven (`max_volume`, `office_names`, `allow_group_volume`).
Shipped defaults are fully permissive. `tests/test_no_rogue_volume.py` fails if any
other module gains a volume call.

## Build from source

Requires Python 3.11+ on Windows (3.13 recommended for packaging).

```
git clone https://github.com/Turrabo/desktop-audio-caster
cd desktop-audio-caster
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pythonw launch_tray.pyw        # run from source
```

Rebuild the distributable exe (regenerates `dist\DesktopAudioStreamer.exe`):

```
.venv\Scripts\pip install pyinstaller
.\scripts\build.ps1
```

There's also a CLI sharing the same engine:

```
.venv\Scripts\python cli.py devices                  # list speakers/groups
.venv\Scripts\python cli.py start "Living Room"      # cast (Ctrl+C stops)
```

Run the tests with `.venv\Scripts\python -m unittest discover -s tests`.
UI design notes: [docs/ui-plan.md](docs/ui-plan.md).

## License

GPL-3.0 — see [LICENSE](LICENSE). Bundles Roboto and Material Icons Round (both
Apache-2.0); see [assets/README.md](assets/README.md).

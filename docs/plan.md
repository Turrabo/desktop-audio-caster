# Desktop Audio Streamer — design and build plan

Replace SamDel's ChromeCast-Desktop-Audio-Streamer with a reliable, simple Windows tray app
that casts whole-device audio to Google Nest speakers and speaker groups.
(Dual-reviewed 2026-07-10; review fixes applied — group-volume rail, format negotiation,
single-clock pacing, network failure modes, HTTP contract.)

## User requirements

- Whole-device audio (not per-tab / per-app).
- Reliability and simplicity above all. Latency: fixed ~2-4 s accepted (D&D ambience, no lip
  sync). The number is measured, not promised, and groups may sit at the high end.
- The laptop must not make audible sound while casting.
- The app ships with NO volume guards (config defaults permissive). Volume restraint applies
  only to Claude-run testing: ≤3%, and no volume ops on "Office" (see project memory
  `testing-volume-etiquette`). safety.py remains as a config-driven choke point.

## Architecture (decided with user)

Cast-a-URL rebuild ("reliable rebuild" option). Python core + system tray UI.

```
WASAPI loopback capture ──► single-clock pacer (real frames | exact-gap silence fill)
        │                                  │
        └── format probe ──► 16-bit PCM convert ──► per-client bounded queue (frame-aligned,
                                                    drop-oldest ~1 s, drops logged)
                                                          │
                                              aiohttp /stream.wav (endless WAV)
                                                          ▲
pychromecast: resident CastBrowser discovery, connect, play_media(url), status, watchdog
```

### Components

| File | Responsibility |
|---|---|
| `streamer/capture.py` | WASAPI loopback of the chosen render device. Probes the device mix format at open; converts to 16-bit PCM at the device's native rate (no resample). Health signal: consecutive read errors or device-invalidated → capture restart; surfaced to watchdog. Handles default-device change (re-open + force media-session restart, since a WAV header can't change mid-flight). |
| `streamer/pacer.py` | ONE monotonic sample clock. Pulls real frames when available; when capture is silent/stopped, synthesizes exactly the missing sample count (no independent timer, no second clock — this is the anti-drift core). |
| `streamer/server.py` | aiohttp on a fixed configurable port. Endless WAV: RIFF/data sizes 0xFFFFFFFF, fresh header per connection, ignore/200 Range requests, no Content-Length. Header fields derived from the ACTUAL capture format. Per-client queue: frame-aligned drop-oldest (~1 s), drop events logged. |
| `streamer/caster.py` | pychromecast wrapper. Resident CastBrowser (zeroconf) so speaker reboots / new IPs / group-leader migration are re-resolved live, not just at connect time. `play_media(url, "audio/wav")`. Watchdog: player state IDLE, sustained BUFFERING (>60 s), capture-health failure, or local IP change → tear down, re-derive stream URL from the current route-to-speaker source IP, re-cast with backoff (2 s → 30 s cap). |
| `streamer/safety.py` | Single choke point for ALL volume ops, enforced not assumed: (1) every `Chromecast` handle is wrapped in a proxy that does not expose `set_volume`/`volume_up`/`volume_down`; (2) config-driven rules — `max_volume` cap, `office_names` protection incl. group-membership resolution via MultizoneController (fail closed), `allow_group_volume` — all permissive in shipped defaults; (3) automated test greps the codebase — `set_volume|volume_up|volume_down` may appear only in safety.py and its tests. |
| `streamer/localmute.py` | Only if the muted-loopback probe passes: mute local endpoint on cast start (pycaw), unmute on stop via try/finally + atexit, PLUS unmute-on-next-start recovery (atexit doesn't run on hard kill). |
| `streamer/tray.py` | pystray: pick device/group, start/stop, quit. Surfaces failures via tray notification ("cast died, retrying…"). Single-instance guard. |
| `streamer/config.py` | `%APPDATA%/desktop-audio-streamer/config.json`: last device, max_volume, port, capture device override, stream_type. Logging to rotating file in same dir. |
| `cli.py` | `devices / start "<name>" / stop / status` — engine fully drivable without tray. |

### Key design decisions

1. **PCM WAV, no encoder** — 16-bit stereo at device-native rate (~1.4-1.5 Mbps, trivial on LAN).
   No LAME/ffmpeg latency or failure modes. Format is probed, never assumed.
2. **No app-side pre-roll.** Only live frames are sent. The speaker's own buffer sets the floor;
   the bounded frame-aligned queue guarantees delay can never grow unbounded (a drop after a
   Wi-Fi stall = one audible skip and re-anchor, logged).
3. **Single-clock continuous stream.** The pacer's monotonic sample counter feeds the stream
   whether or not real audio is playing — keep-alive, no receiver underrun, and no two-clock drift.
4. **streamType**: trial `LIVE` vs `BUFFERED` against a real speaker; sustained-BUFFERING wedge
   counts as failure for the LIVE trial. Winner recorded in config; must survive a 30-min soak.
5. **Reconnect = pychromecast socket healing + resident discovery + one watchdog** (see caster.py
   row). Explicitly covered: sleep/resume (capture invalidated + IP re-check on resume), DHCP/IP
   change (URL re-derived per re-cast), speaker reboot (browser re-resolves), group leader
   migration (browser re-resolves), firewall (below).
6. **Firewall/NIC**: setup adds an inbound allow rule for the venv's python.exe (netsh, elevated —
   UAC is off on this machine). Source IP for the URL is derived by opening a UDP socket toward
   the speaker's IP and reading the local address (route-based, correct across multiple NICs/VPNs).
   Pre-cast self-check: after `play_media`, if zero GETs arrive within 10 s → report "firewall or
   routing" explicitly instead of generic failure.
7. **Silent machine**: probe muted-endpoint loopback empirically (behaviour varies by driver).
   Pass → localmute.py path. Fail → VB-Cable as the supported silent path (documented install,
   endpoint volume pinned 100%, app captures the cable). Decision recorded in README; the probe
   result also decides whether localmute.py is built at all.
8. **Groups are devices** via the group leader. Group volume ops go through safety.py's
   config-driven rules like any other volume op; normal app operation performs no volume
   writes at all (the user drives volume via Google Home / the speakers themselves).

### Dependencies

`pychromecast`+`zeroconf`, `pyaudiowpatch` (WASAPI loopback; fallback `soundcard`), `aiohttp`,
`pystray`+`Pillow`, `pycaw`+`comtypes` (only if mute path chosen).

**Step 0**: create venv and install ALL deps first. pyaudiowpatch/pycaw wheels may lag
CPython 3.14 → if any fail, pin venv to 3.12 before writing code.

## Build order

0. venv + full dependency install (decides 3.14 vs 3.12 immediately).
1. Probe scripts (scratch, deleted after): (a) loopback format + muted-capture behaviour —
   local only; (b) pychromecast discovery listing devices/groups (read-only).
2. `safety.py` + its tests FIRST — including the group-membership Office rule and the grep test —
   before any code can touch a speaker.
3. `capture.py` + `pacer.py` + `server.py`: verify locally — byte-inspect `/stream.wav` from
   another process; confirm header matches probed format; confirm silence fill keeps byte rate
   exactly at nominal (sample-count check over 60 s).
4. Firewall rule + cross-check: fetch the stream URL from another LAN host (phone browser works).
5. `caster.py` + `cli.py`: cast to one real speaker (current 1% volume). LIVE vs BUFFERED trial.
   Verify `status.volume_level` unchanged after DMR launch. Tonight's checks are status-API only
   (PLAYING sustained, GETs flowing, byte counters advancing).
6. Watchdog + 30-min soak (status PLAYING throughout; drop/reconnect counters at zero).
7. `tray.py` + config + notifications.
8. **Daytime gate (user, audible volume): audio actually sounds right (no pitch/noise garbage),
   real latency measured solo + in a group.** Not "done" until this passes — status APIs cannot
   detect corrupted audio.
9. README, `git init`, commits.

## Testing constraints (standing)

Apply to Claude-run tests only — the app itself is unguarded:

- Any `set_volume` in a Claude test: ≤ 0.03 via an explicit restrictive cfg, non-Office speaker.
- "Office": no volume operation from Claude tests, direct or via group membership. Casting to a
  group containing it is fine.
- Audible checks (sound quality, real latency) are the user's daytime gate — status APIs cannot
  hear.

## Out of scope (this build)

- Sub-second mirroring (openscreen port) — possible later project; this app is the fallback.
- Multi-PC, non-Google devices, EQ/resample options, per-app capture.

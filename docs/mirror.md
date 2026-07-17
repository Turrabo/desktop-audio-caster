# Mirror mode - design reference

Mirror mode is the app's low-latency cast path: it speaks the Google Cast
mirroring protocol (the same one Chrome uses for tab casting) directly to the
speaker, reaching sub-second latency where the HTTP path lands around a second.
It is the default when the setup supports it and falls back to the HTTP path
otherwise. Measured latency figures are in the README.

## Two paths, one contract

`AppController` owns a session that is either a `CastSession` (HTTP / Default
Media Receiver, in `streamer/caster.py`) or a `MirrorSession` (mirroring, in
`streamer/mirror.py`). `MirrorSession` deliberately exposes the same surface -
`start` / `stop` / `lag_seconds` / `trim_count` / `recast_count` / `safe_cast`
and the `on_state(state, detail)` contract, including the `"{name}|{lag:.1f}"`
PLAYING detail the popover and tray parse - so the UI, the CLI, and the volume
manager need no mirror-specific code.

`cast_mode` (config, default `auto`) selects the path: `auto` mirrors when
eligible and falls back to HTTP; `mirror` forces it (still falls back if
ineligible); `http` pins the original path and is the kill switch.

## Pipeline

```
capture.py (WASAPI loopback, 48 kHz s16) -> pacer.py (single clock, unchanged)
   -> AppController._pacer_sink fan-out:
        server.feed  (HTTP path; a no-op with no clients, e.g. while mirroring)
        MirrorSession.feed -> MirrorSink
MirrorSink: reframes the pacer's variable-size chunks into exact 480-sample
   (10 ms) Opus frames, encodes on a pump thread, hands packets to CastRtpSender
CastRtpSender: AES-128-CTR per frame -> Cast RTP over UDP; RTCP Sender Reports;
   parses Cast Feedback (checkpoint, playout delay, NACKs) and retransmits
```

The pacer stays the single clock, so latency is fixed and drift-free on both
paths (the app's original anti-drift design).

## Protocol essentials

- Signaling: OFFER/ANSWER on `urn:x-cast:com.google.cast.webrtc`, carried over
  pychromecast's existing CASTV2 channel. The OFFER launches the audio-only
  mirroring receiver app (falling back to the A/V one) and advertises one Opus
  stream (48 kHz stereo, 10 ms frames) with a sender-chosen target playout
  delay and sender-generated AES key + IV mask.
- Media: one Opus frame per RTP packet, AES-128-CTR encrypted (nonce = IV mask
  XOR the frame id at byte 8). The first Sender Report is sent before frame 0 -
  the receiver drops RTP until it has the lip-sync anchor.
- Feedback: the receiver's Cast Feedback carries a checkpoint frame id
  (truncated to 8 bits), its current playout delay, and packet NACKs. The
  sender retransmits NACKed packets (rate-limited) and expands the 8-bit
  checkpoint against the last frame sent.

Protocol behaviour is adapted from
[chromecast-sink](https://github.com/Nerahikada/chromecast-sink) (MIT) and
cross-checked against [chromium/openscreen](https://github.com/chromium/openscreen)
(reference only); see `assets/README.md` for the shipped attribution.

## Eligibility

Mirror requires 48 kHz stereo 16-bit capture (the fixed Opus config; no
resampler yet) and a loadable `opus.dll`. The check is `mirror.eligible_format`
+ `AppController._mirror_available`; anything else routes to HTTP with a log
line. 48 kHz is the usual Windows shared-mode default.

## Reliability and fallback

- Start-time failure (the session never reaches PLAYING - e.g. the receiver
  refuses the stream, or the protocol has changed) raises from `start()` and
  `AppController` falls straight to HTTP. No retry: a broken protocol is
  deterministic, and retrying would leave the user muted and unable to press
  stop during the storm.
- Mid-session loss (was PLAYING) is recovered by re-OFFER with backoff, up to a
  bounded number of attempts, before falling back - so a transient Wi-Fi blip
  does not cost the HTTP latency regression.
- The watchdog (`MirrorSession._failure_reason`) triggers on: capture
  unhealthy, capture format changed, receiver app changed, local IP changed,
  RTCP silence, and a wedged-receiver check on the raw checkpoint byte freezing
  (a stalled receiver that still sends RTCP re-expands its stale checkpoint to
  look like progress, so only the untranslated wire byte catches it). A capture
  format change is permanent (it also invalidates the HTTP WAV header), so it
  triggers a clean full-pipeline restart rather than an in-place swap.

The timing constants for all of the above live at the top of
`streamer/mirror.py`.

### Privacy

While mirroring, the HTTP server stays bound (for fast fallback) but returns
503 for `/stream.wav` (`StreamServer.serving`), so the encrypted mirror stream
is never shadowed by a cleartext WAV any subnet host could fetch. Mirror is a
net privacy improvement over the HTTP path, which serves cleartext to the LAN.

## Key decisions

- **Crypto via Windows CNG, not a Python package.** AES-128-CTR runs on
  `bcrypt.dll` (`streamer/_aesctr.py`), so nothing crypto-shaped ships in the
  exe. Validated byte-for-byte against a known-answer vector in the tests.
- **opus.dll is a static-CRT MSVC build** depending only on KERNEL32 (no VC++
  redistributable needed); provenance + SHA-256 in `assets/README.md`.
- **PyAV is spike-only.** The app encodes via ctypes -> opus.dll; PyAV (used by
  the `experiments/mirroring` probes) would triple the exe and is excluded from
  the build.

## Platform risk

Mirroring rides a reverse-engineered protocol, an internal receiver app id, and
a payload-type hack openscreen's own source says Google intends to remove. A
firmware or server-side change could break it with no notice. The automatic
HTTP fallback is the mitigation; `cast_mode: http` is the explicit escape.

## Out of scope

44.1 kHz resampling, adaptive playout delay below the default, multi-device
simultaneous mirror, video, and a UI toggle for `cast_mode` (config-file only).

## Development probes

`experiments/mirroring/` holds hardware probe harnesses that import the shipped
`streamer.mirror` (a media-free OFFER/ANSWER probe, a synthetic-source transport
probe, and a real-desktop-audio harness for the audible/latency check). They are
dev-only and not part of the shipped app; see that folder's `THIRD_PARTY.md`.

# Mirror mode - integrate the Cast mirroring path into the app

Status: REVISED after dual-review (2 independent reviewers, 2026-07-17).
Convergent findings folded in; decisions recorded inline. Implements the
spike result (experiments/mirroring, commit b560d64) as the app's primary
cast path with automatic HTTP fallback.

## Evidence base (spike, 2026-07-17, real hardware)

- Audio-only mirroring receiver 85CDB22F launches and ANSWERs on a solo Nest
  Audio AND the Everywhere group; 10-min soak: checkpoint lag 1-3 frames,
  0 NACKs, receiver playout delay pinned at 400 ms (vs ~1.1 s HTTP floor).
- NOT yet proven: audible correctness (checkpoints fire pre-decryption),
  group member sync, glass-to-glass number. These gate ROLLOUT at milestone
  2 (real desktop audio, user ears), before the heavy build.
- Firewall/security, VERIFIED on this machine (reviewer B): the spike's real
  process image (C:\Python314\python.exe, the venv redirector target) has
  ZERO firewall rules, yet RTCP flowed through the whole soak on the Public
  profile under BlockInbound. Receiver RTCP is solicited return traffic on
  the outbound UDP 5-tuple, admitted ruleless by Windows stateful filtering.
  So mirror mode needs NO inbound firewall rule and SHRINKS the firewall
  failure surface (the never-fetched firewall ERROR is reachable only via the
  HTTP fallback, where its message stays accurate). The frozen exe also
  already carries the v1.0.1 localsubnet rule. CERT_NONE on the pychromecast
  TLS channel is NOT a regression: the HTTP path streams cleartext WAV to the
  whole subnet today; mirror sends AES-CTR audio and exposes the key only to
  an active MITM of the TLS channel, with fresh keys per attempt. Net
  security improvement; the README note will say so.
- Platform risk stated plainly: reverse-engineered protocol, internal app ID,
  PT 127 AndroidTV hack openscreen wants to remove. Mitigation = automatic
  fallback to the HTTP path, which stays fully supported; cast_mode=http is
  the explicit kill switch.

## Architecture

```
capture.py (48 kHz s16) -> pacer.py (variable-size ticks, unchanged)
                              '-> fan-out sink (appctl composes):
                                    |-> server.feed (HTTP path, kept warm)
                                    '-> MirrorSink.feed (when mirroring)
MirrorSink: reframing FIFO -> exact 480-sample Opus frames -> CastRtpSender
appctl.py: _do_start picks MirrorSession or CastSession by mode+eligibility;
           MirrorSession.on_fallback_needed -> _do_fallback op (distinct from
           the firewall ERROR path) swaps to HTTP without unmuting.
streamer/mirror.py (new): OpusEncoder (ctypes, assets/opus.dll), FrameCrypto
           (ctypes BCrypt - no new pip dep), the RTCP feedback parser, the
           signaling controller, MirrorSession. Protocol logic gets ONE home
           here; experiments/mirroring shrinks to the probe harnesses, which
           re-import from streamer.mirror.
```

### Decision: crypto ships via Windows BCrypt (no new pip dependency)

Reviewers C1/C3: `cryptography` (AES-128-CTR) would become a shipped runtime
dep, contradicting experiments/mirroring/requirements.txt and growing the exe
~5-8 MB. Instead FrameCrypto uses ctypes over bcrypt.dll (present on every
Windows box, zero bundle cost): AES-ECB a 16-byte counter block, XOR with the
payload, increment the 128-bit counter big-endian per block - textbook CTR,
cipher itself is CNG, we only assemble the mode. The existing offline
known-answer + round-trip tests (test_mirroring_crypto.py) validate it byte
-for-byte against the openscreen construction; they retarget streamer.mirror.
frame_crypto's public interface (nonce/encrypt/decrypt) is preserved so the
promotion is mechanical. `cryptography` stays out of root requirements.

### Decision: opus.dll built from source (provenance recorded)

Built locally from the official opus 1.5.2 release with MSVC 2022 (UCRT, so
no MinGW libgcc/winpthread runtime deps - reviewer A-S7); DLL is 451 KB,
`opus_get_version_string()` == "libopus 1.5.2", one-frame encode verified via
ctypes. Tarball + DLL SHA-256 recorded in assets/README.md. (PyOgg-wheel
extraction was the fallback and also worked, but the source build has clean
provenance and no third-party runtime deps.) PyAV stays a spike-only dep
(av.libs is 62.6 MB - tripling the exe is unacceptable).

### Eligibility (v1)

Mirror requires: capture format 48000 Hz / 2 ch / 16-bit, opus.dll loadable,
mirror module importable. Else -> HTTP path with a log line stating why. No
resampler in v1 (this machine and most modern shared-mode endpoints mix at
48 kHz; 44.1 kHz polyphase is a follow-up). Eligibility is re-checked on the
mid-session format-change trigger below.

### Encoder (ctypes, ~60 lines)

opus_encoder_create(48000, 2, OPUS_APPLICATION_RESTRICTED_LOWDELAY=2051),
opus_encoder_ctl(SET_BITRATE=4002, 128000), (SET_DTX=4016, 0). Care points
(reviewer A-S7): opus_encoder_ctl is variadic - pass explicitly-typed c_int
args, do NOT fix argtypes; opus_encode returns negative error codes - check
every call. assets/opus.dll bundled via the .spec `binaries` (dependency
-scanned), not `datas`. Frozen-aware load path via a SHARED asset helper
hoisted out of streamer/ui/fonts.py (reviewer A-N3: mirror.py must not import
the UI package). M1 unit test asserts the version string + a sane encode.

### MirrorSession - FULL contract (mirrors CastSession so appctl/UI/cli need no new code paths)

Members appctl/popover/tray/cli actually touch (reviewers A-C4, B-S1), all
required:
- constructor: same shape as CastSession (discovery, SafeCast, capture,
  on_state, sent-seconds/lag source), plus on_fallback_needed.
- `safe_cast` property -> the SafeCast (VolumeManager reuses it for the active
  card's volume slider; safety choke stays intact - test_no_rogue_volume must
  stay green, so MirrorSession only ever holds a SafeCast, never a bare
  Chromecast).
- `lag_seconds()` method, `trim_count`, `recast_count` attributes (cli.py
  status reads all three every 10 s; missing any -> AttributeError kills the
  CLI loop).
- `on_state(state, detail)` emitting the EXACT formats the UI parses:
  PLAYING detail = f"{name}|{lag:.1f}" (popover and tray split on `|`);
  transitional states carry detail=device-name. Emissions are DEDUPED like
  CastSession (caster.py pattern) so listeners are not flooded per poll.
- "compatibility mode" annotation (after fallback) rides the NAME segment of
  the PLAYING detail, never after the lag field (else the lag render corrupts;
  it also lands in the tray tooltip - acceptable).
- Lag value = the receiver's LIVE playout delay from Cast Feedback
  (FeedbackStats.playout_delay_ms), not the static 400 config constant
  (reviewer B-N1: echoing the config value would be fake telemetry). Roughly
  commensurable with the HTTP path's sent-minus-played (both receiver-side).

States: LAUNCHING -> BUFFERING (ANSWER ok, awaiting first checkpoint) ->
PLAYING (checkpoint advancing). All already in TRANSITIONAL_STATES / STATE_TEXT
(verified), so "no new UI states" holds. WAITING_STREAM simply never emitted
(harmless - popover uses a dict lookup). CastSession's 60 s BUFFERING wedge
timer is internal to CastSession, not shared - no conflict.

### MirrorSession - lifecycle and reliability

- Feed path: MirrorSink.feed is non-blocking (called on the pacer thread);
  it appends to a bounded frame-aligned drop-oldest FIFO (~0.3 s, like server
  clients) and a dedicated feed thread drains it, REFRAMES to exact 480-sample
  frames (reviewers A-S1, B-S4: pacer ticks are variable-size, NOT 960 samples
  - `need` is recomputed from the monotonic clock each tick; MirrorSink must
  accumulate and carve 480-sample frames with a carried remainder), encodes,
  send_frame. First SR synchronous before frame 0 + frame-0 re-sends
  (spike-proven).
- NACK retransmission (production requirement the spike skipped): ring of the
  last 128 sent packets keyed by frame_id & 0xFF; on Cast Feedback NACKs,
  re-send matching packets, rate-limited one re-send per frame per 100 ms.
  This ALSO restores the expand_frame_id precondition (<=~120 in flight) that
  the desync guard below relies on.
- First-checkpoint deadline (reviewer B-S5): if no checkpoint arrives within
  3 s of the ANSWER, the attempt has failed (checkpoint == -1 before the first
  one, so "lag > N" is undefined until then; and "RTCP silence" won't trip if
  the receiver sends bare receiver-reports without Cast Feedback - datagrams !=
  feedbacks).
- Desync detection on the RAW WIRE VALUE (reviewer A-C3): the fatal case is a
  wedged receiver that still sends RTCP - its stale 8-bit checkpoint re
  -expands to look like forward progress, evading a lag threshold. Trigger on
  the truncated checkpoint byte being UNCHANGED for > 2 s while frames are
  being sent, not on expanded-lag magnitude.
- Watchdog (5 s period, mirrors CastSession) -> teardown + relaunch + re-OFFER
  (fresh keys) with 2->30 s backoff, on ANY of: RTCP silence > 6 s; raw
  checkpoint stalled > 2 s (above); receiver app_id changed; capture unhealthy;
  local IP changed; capture.format != negotiated 48 kHz/2ch (reviewers A-S3,
  B-C2: sleep/resume or default-device switch to 44.1 kHz otherwise feeds the
  fixed 48 k encoder wrong-rate PCM -> ~9% fast, pitch-shifted, forever, with
  no other trigger catching it; this path re-runs eligibility and falls to
  HTTP if now ineligible). While in there, fix capture.py's `format_changed`
  comment - it references a surface that does not exist anywhere (grep-null).
- stop(): stop threads -> quit_app -> close socket; joins kept SHORT (appctl
  hard-exits at 6 s - reviewer A-N7). Mute restore stays appctl's job.
- Zero volume calls anywhere.

### Fallback - designed, not just named (reviewers A-C2, B-C1, S1)

The naive "on_gave_up -> stop+start HTTP" is wrong three ways: _do_stop
unmutes the PC first (audible desktop pop), tears down the warm server, and
clears cast_target (the popover's spinning-card key); the callback fires from
a watchdog thread and races a user stop/switch already in the op queue; and
reusing _on_session_gave_up collides with the v1.0.1 firewall ERROR
semantics. Design:

- START-time failure (session never reached PLAYING) -> fall back IMMEDIATELY,
  ONE mirror attempt with a hard ~8 s overall deadline (launch 5 s, ANSWER
  4 s, first checkpoint within 3 s), then straight to HTTP, zero backoff
  (reviewer B-C1). Rationale: the headline threat ("Google changed
  something") is deterministic - a second attempt learns nothing - and the
  naive 2-attempts+backoff policy produced a ~30-55 s window of MUTED silence
  with every button disabled (busy()==true) so the user cannot even press
  stop. New worst-case start silence ~21 s, typical mirror-broken ~17 s,
  mirror-working ~4-6 s (beats HTTP's 11-13 s). Remember the failure
  (session/config flag) so the NEXT play press goes straight to HTTP; re-arm
  mirror via a media-free background OFFER probe (probe_answer already shows
  the shape), never by burning the user's start.
- MID-session loss (mirror WAS PLAYING) -> keep the 2-attempts-with-backoff
  re-OFFER recovery: there a ~5-8 s re-OFFER genuinely beats an HTTP swap
  (~13 s + permanent 1.1 s regression) and transient Wi-Fi is plausible. Two
  failures -> fall back to HTTP.
- Mechanism: a DEDICATED `on_fallback_needed` callback (distinct from
  `_on_session_gave_up`, which must keep meaning ERROR+teardown+tray-toast for
  the post-fallback CastSession) -> enqueues a dedicated `_do_fallback` op
  that: stops ONLY the mirror session, KEEPS mute engaged, preserves
  cast_target, reuses/reconnects the SafeCast, and builds the HTTP CastSession
  with the FULL kwargs set incl. fetch_count_fn/sent_seconds_fn/on_gave_up
  (reviewer B-S1: omitting fetch_count_fn makes _never_fetched() always False,
  so a firewall-blocked fallback loops RECONNECTING forever with the popover
  locked - the exact wedge commit 713938f killed). The op is STALE-GUARDED:
  no-op unless self.session is still the exact MirrorSession that gave up
  (guards user-stop-then-fallback and switch-device-then-fallback races). The
  give-up path must NOT emit ERROR before falling back (no red banner + toast
  that PLAYING then erases).

### Privacy: gate the warm HTTP server while mirroring (reviewer B-S2)

The server stays bound for fast fallback, but serving /stream.wav during a
mirror session would leak cleartext desktop audio to any subnet host while the
user believes the stream is encrypted. Add a mode gate: /stream.wav returns
503 while mirror is active, flips to serving on fallback (~10 lines in
server.py). Fallback only needs the socket bound, not content served.

### config / cli / UI

- config: cast_mode "auto" | "mirror" | "http" (default auto),
  mirror_target_delay_ms 400. Both go in DEFAULTS and are USER-POLICY keys -
  kept OUT of APP_OWNED_KEYS (reviewer A-S5: save() persists app-owned keys on
  every cast, so a one-off `cli start --mode http` would silently rewrite the
  tray default and flip the kill switch; stream_type's persist-pattern is
  grandfathered, do not copy it).
- target delay clamped against the ANSWER's constraints min/max when present
  (reviewer A-N5), not blindly trusted from config.
- cli.py: --mode on start; status prints CONFIGURED vs RESOLVED path (so a
  forced --mode mirror silently downgraded to HTTP is visible - reviewer N4).
- UI: no new controls; existing state machine renders mirror sessions as-is.

## Milestones (each independently testable; user gates in bold)

1. Shared asset helper hoist; assets/opus.dll + streamer/mirror.py OpusEncoder
   + FrameCrypto (BCrypt) + promoted parser/controller; retarget the two
   mirroring unit tests + both probe harnesses at streamer.mirror; move the
   MIT attribution into streamer/mirror.py. Unit tests: encoder version+encode,
   crypto KAT/round-trip (both green before hardware). ~3 hr
2. MirrorSession core (feed/reframe/SR/first-checkpoint; NO NACK/watchdog yet)
   + a harness casting REAL desktop audio through the SAME MirrorSink+Pacer
   plumbing that ships (own that this is "half of milestone 4"). Local mute
   engaged (or an explicit volume-pin instruction) so the ear test isn't
   polluted by unmuted local audio 0.4 s ahead. I verify transport silently,
   log nack_events, then **user ear gate: real music on Dining Room solo then
   Everywhere - correct sound, all rooms, in sync** + the click-track
   glass-to-glass measurement (opus_source content="click") so the 400 ms
   claim ships MEASURED. Pre-agreed: an isolated glitch WITH NACKs logged =
   packet loss that M3 fixes, re-run; it does not fail the gate (reviewer
   B-N3). ~4 hr
3. NACK ring + watchdog (all triggers incl. format-change + raw-checkpoint
   stall) + re-OFFER + the fallback machinery (on_fallback_needed, _do_fallback,
   stale-guard, server 503 gate), with unit tests on a fake clock + synthetic
   feedback. ~5 hr
4. appctl/config/cli integration; popover casts via mirror end-to-end incl.
   the start-time-immediate-fallback and mid-session-recovery policies;
   30-min soak while the user uses the machine normally. **User gate:
   day-to-day feel, latency, stability, and a deliberate mid-cast
   headphone-switch to confirm graceful fallback.** ~3 hr
5. Tests complete + doc sweep (grep-explicit: README.md:45-48 "no encoder"/
   "different project", docs/plan.md:11-13,120 mirroring-as-future, ui-plan.md
   CastSession/MirrorSession note; opus BSD-3 full text + chromecast-sink MIT
   into assets/README.md as the shipped third-party manifest) + .spec binaries
   entry for opus.dll + version 1.0.1->1.1.0 (4 spots in version_info.txt) +
   exe rebuild + smoke. Release AFTER the user has lived with it a day. ~3 hr

Total ~3 days human-equivalent (reviewers' estimate; my first pass of ~2 days
under-counted the crypto decision, probe/test/licence rewiring, format guard,
and fallback rework). experiments/ protocol modules are deleted only at
milestone 4 (after the probes/tests are retargeted and green), so logic has
exactly one home.

## Risks and mitigations

- Google server-side change kills mirroring -> immediate HTTP fallback at
  start; cast_mode=http kill switch; README says so.
- Group member desync only detectable by ear -> milestone 2 gate before the
  integration build.
- Frame-id 8-bit desync -> NACK send-window keeps <120 in flight; watchdog
  raw-checkpoint-stall trigger; both bound it.
- Mid-session 44.1 kHz -> format-change watchdog trigger -> re-eligibility ->
  HTTP.
- opus.dll load/arch -> hash recorded, MSVC/UCRT build (no runtime deps),
  startup load-test + eligibility fallback; M1 test asserts version string.
- Live app disruption during dev -> all live tests coordinated with the user;
  the running tray app is never restarted mid-cast without warning.

## Out of scope (recorded, not planned)

44.1 kHz resample, adaptive playout delay below 400 ms, kickstart (continuous
100 fps audio never idles), multi-device simultaneous mirror, video, a config
UI toggle for cast_mode (config-file only in v1).

# UI sweep plan — popover, volume, status feedback

User requirements (2026-07-11):
1. Visual feedback during status changes — must never feel hung.
2. Volume control for speakers AND groups from the UI.
3. Clicking the tray icon must NOT cast; it opens a popover to choose a target.
4. Popover clearly divided: GROUPS section, SPEAKERS section.
5. Start-with-Windows option and Exit.

(Dual-reviewed 2026-07-11; mechanisms below incorporate the review fixes:
focus-out strategy, single UI queue, group-resolve short-circuit, connection
lifecycle, slider echo suppression, op serialization, BUFFERING/firewall
states, DPI, cli port, fast exit.)

## Architecture

```
streamer/appctl.py     AppController: owns Discovery, pipeline, CastSession,
                       LocalMute, VolumeManager. ONE worker thread with an op
                       queue - start/stop/switch/volume are serialized; UI
                       buttons disable during transitional states.
                       Emits state events into the UI queue.
streamer/ui/popover.py tkinter UI. ONE queue.Queue drained by a 50 ms
                       root.after poll loop owned by the Tk main thread; every
                       other thread (pystray, pychromecast listeners, workers)
                       only ever q.put(callable). No direct tk calls off-thread.
streamer/startup.py    HKCU Run key (quoted venv pythonw + launch_tray.pyw),
                       self-repairing: enable() always rewrites the current
                       correct command; app start rewrites a stale entry.
launch_tray.pyw        Shim at repo root: fixes sys.path + cwd, runs tray main.
streamer/tray.py       pystray icon only. Left-click (default) -> enqueue
                       popover toggle. Right-click: Open, Exit. Icon appears
                       IMMEDIATELY (no discovery sleep; rows fill via events).
```

## State machine (drives all visual feedback)

IDLE / DISCOVERING / CONNECTING / LAUNCHING / WAITING_STREAM / BUFFERING /
PLAYING / RECONNECTING(reason, attempt) / STOPPING / ERROR(msg).

- Tray glyph: grey IDLE, amber transitional (incl. BUFFERING, RECONNECTING),
  blue PLAYING, red ERROR.
- Popover header: state-tinted cast icon + text ("Casting to Everywhere",
  "Speaker buffering…", "Reconnecting: …"); errors surface in a wrapping
  error-container banner under the header; the active card shows an accent
  badge and "Casting · lag X s" subtitle.
- CastSession gains on_state + fetch_count_fn + on_gave_up: if no stream GET
  ever arrives, bounded re-casts then a persistent firewall ERROR with
  controller teardown (unmute, clear target) - never an endless spinner.
- Sustained BUFFERING surfaces immediately (not hidden for the 60 s wedge
  window). RECONNECTING shows attempt count so endless retry never looks hung.
- PC-muted indicator shown in the popover while local mute is engaged.

## Popover mechanics (Windows-proofed)

- overrideredirect + topmost; after deiconify: focus_force(); FocusOut handler
  closes ONLY if focus_get() is no longer inside this toplevel's widget tree
  (slider/checkbox focus changes don't close it); ESC closes; tray click
  toggles.
- DPI: per-monitor awareness (v2 context, shcore/user32 fallbacks); every
  metric and font size scales via dp() from the DPI of the monitor under the
  cursor at show() time; content taller than the work area scrolls inside a
  height-capped region.
- Position near cursor, clamped to the current monitor's WORK AREA via
  MonitorFromPoint + GetMonitorInfo (ctypes) - never under the taskbar or
  off-screen.
- Rows rebuild on DEVICES_CHANGED (real discovery add/remove listener, not the
  discard-lambda); a vanished device's row greys out.

## Volume design

- All writes via safety.set_volume (choke point kept). safety change: when
  office_names is empty there is nothing to protect -> skip group member
  resolution entirely (no 5 s round-trip); when non-empty, cache one
  MultizoneController per cast object (no per-call handler leak).
- VolumeManager: connections live only while the popover is open - parallel
  connects (<=4 workers, 5 s timeout) on open, volume levels populate sliders
  as they arrive (disabled until then), all closed on popover close. The
  active cast target reuses the CastSession connection (never double-connect).
- Slider: 0..100 scaled to 0..max_volume; 250 ms debounce out; inbound volume
  echoes ignored while pressed and for 1.5 s after last write (no snap-back).
- Group volume = leader write; Google rescales members (user's choice).

## Reliability fixes bundled in this pass

- _recover() disconnects the old cast before replacing it (pre-existing
  socket-client thread leak, amplified by re-casts).
- Exit: mute restore FIRST (fast, local), then background teardown with a hard
  os._exit after 6 s - Exit never feels hung; daemon threads make this safe.
- Single-instance guard port doubles as control channel: a second launch sends
  SHOW and exits, first instance pops the popover (autostart + manual launch
  no longer a silent no-op).
- cli.py ports onto AppController (pipeline wiring has exactly one home).

## Testing constraints (Claude-run)

Volume live-test only on a single non-Office speaker, <=0.03, restore prior
level. Group volume not live-tested by Claude ("Everywhere" contains Office);
code path identical (leader write), user verifies audibly.

## Out of scope

Per-member group volume, EQ, multiple simultaneous targets, packaging.

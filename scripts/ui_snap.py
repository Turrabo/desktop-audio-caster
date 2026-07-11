"""Render the popover with a fake controller and screenshot it (self-grab).
Three states: idle, casting, error. No live app involved.

Usage: ui_snap.py [out_prefix] [scale]
scale (e.g. 1.5) exercises the DPI plumbing via DAS_UI_SCALE; grabs are
taken after the entry fade completes (alpha restored to 1.0). Also times
the slider render loop at that scale."""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OUT = sys.argv[1] if len(sys.argv) > 1 else "ui_snap"
if len(sys.argv) > 2:
    os.environ["DAS_UI_SCALE"] = sys.argv[2]

from PIL import ImageGrab

from streamer.ui.popover import Popover

MARGIN = 48   # wide enough to inspect the DWM shadow


class FakeVolumes:
    known_levels = {"Everywhere": 0.35, "Dining Room": 0.20, "Kitchen": 0.01,
                    "Living Room": 0.01, "Chromecast New": 0.55}
    last_write = {}

    def open_sweep(self, names, on_level):
        pass

    def set_volume_debounced(self, name, level):
        print(f"volume write: {name} -> {level:.2f}")

    def close_all(self):
        pass


class FakeCtl:
    state = "IDLE"
    state_detail = None
    cast_target = None
    volumes = FakeVolumes()

    def add_listener(self, cb):
        pass

    def devices(self):
        return {
            "groups": [{"name": "Everywhere", "type": "group"}],
            "speakers": [
                {"name": "Chromecast New", "type": "cast"},
                {"name": "Dining Room", "type": "audio"},
                {"name": "Kitchen", "type": "audio"},
                {"name": "Living Room", "type": "audio"},
            ],
        }

    def busy(self):
        return self.state not in ("IDLE", "PLAYING", "ERROR")

    def start_cast(self, name):
        pass

    def stop_cast(self):
        pass


ctl = FakeCtl()
pop = Popover(ctl, on_exit=lambda: None)

# Deterministic position: pin by faking the pointer position (the popover
# reads DPI and work area through the same coordinates).
pop.root.winfo_pointerxy = lambda: (600, 700)


def grab(tag):
    pop.win.update_idletasks()
    x, y = pop.win.winfo_rootx(), pop.win.winfo_rooty()
    w, h = pop.win.winfo_width(), pop.win.winfo_height()
    img = ImageGrab.grab(bbox=(x - MARGIN, y - MARGIN,
                               x + w + MARGIN, y + h + MARGIN))
    path = rf"{OUT}_{tag}.png"
    img.save(path)
    print(f"saved {path} ({w}x{h})")


def bench_slider():
    s = next(iter(pop._rows.values()))["slider"]
    t0 = time.perf_counter()
    for i in range(101):
        s.set_value(i)
        s.update_idletasks()
    dt = (time.perf_counter() - t0) / 101 * 1000
    print(f"slider render: {dt:.2f} ms/frame at scale {pop.scale}")
    s.set_value(35)


def scenario():
    pop.show()
    pop.root.after(700, bench_slider)
    pop.root.after(900, lambda: grab("idle"))

    def casting_state():
        ctl.state, ctl.state_detail = "PLAYING", "Everywhere|1.1"
        ctl.cast_target = "Everywhere"
        pop._apply_state("PLAYING", "Everywhere|1.1")

    def error_state():
        ctl.state = "ERROR"
        ctl.state_detail = ("Everywhere never fetched the stream - "
                            "check the firewall allows inbound connections "
                            "on the stream port")
        ctl.cast_target = None
        pop._apply_state("ERROR", ctl.state_detail)

    pop.root.after(1100, casting_state)
    pop.root.after(1500, lambda: grab("casting"))
    pop.root.after(1700, error_state)
    pop.root.after(2100, lambda: grab("error"))
    pop.root.after(2400, pop.root.destroy)


pop.root.after(300, scenario)
pop.root.mainloop()
print("done")

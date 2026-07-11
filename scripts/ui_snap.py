"""Render the popover with a fake controller and screenshot it (self-grab).
Two states: idle and casting. No live app involved."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import ImageGrab

from streamer.ui.popover import Popover

OUT = sys.argv[1] if len(sys.argv) > 1 else "ui_snap"


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

# Deterministic position: pin to 60,60 by faking the pointer position.
pop.root.winfo_pointerxy = lambda: (260, 700)


def grab(tag):
    pop.win.update_idletasks()
    x, y = pop.win.winfo_rootx(), pop.win.winfo_rooty()
    w, h = pop.win.winfo_width(), pop.win.winfo_height()
    img = ImageGrab.grab(bbox=(x - 6, y - 6, x + w + 6, y + h + 6))
    path = rf"{OUT}_{tag}.png"
    img.save(path)
    print(f"saved {path} ({w}x{h})")


def scenario():
    pop.show()
    pop.root.after(900, lambda: grab("idle"))

    def casting_state():
        ctl.state, ctl.state_detail = "PLAYING", "Everywhere|1.1"
        ctl.cast_target = "Everywhere"
        pop._apply_state("PLAYING", "Everywhere|1.1")

    pop.root.after(1200, casting_state)
    pop.root.after(2000, lambda: grab("casting"))
    pop.root.after(2400, pop.root.destroy)


pop.root.after(300, scenario)
pop.root.mainloop()
print("done")

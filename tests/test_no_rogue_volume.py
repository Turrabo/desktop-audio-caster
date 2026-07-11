"""Enforcement test: volume calls exist ONLY in streamer/safety.py.

Guards the safety choke point structurally - a new call site anywhere else in
the package fails CI, not code review.
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERN = re.compile(r"\.(set_volume|volume_up|volume_down)\s*\(")


class TestNoRogueVolumeCalls(unittest.TestCase):
    def test_only_safety_module_touches_volume(self):
        offenders = []
        for py in (ROOT / "streamer").rglob("*.py"):
            if py.name == "safety.py":
                continue
            for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                if PATTERN.search(line):
                    offenders.append(f"{py.relative_to(ROOT)}:{lineno}: {line.strip()}")
        for py in [ROOT / "cli.py"]:
            if py.exists():
                for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                    if PATTERN.search(line):
                        offenders.append(f"{py.name}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [], "volume calls outside safety.py:\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()

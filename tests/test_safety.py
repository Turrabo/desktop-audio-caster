"""Safety layer tests - run before any code may touch a speaker."""
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from streamer import safety


def fake_cast(name="Kitchen", cast_type="audio", uuid="u-1"):
    cast = mock.MagicMock()
    cast.name = name
    cast.cast_type = cast_type
    cast.uuid = uuid
    return cast


# Tests exercise the MECHANISM with restrictive rules; the shipped defaults
# are permissive (config.DEFAULTS) since the user lifted the testing-phase
# guards on 2026-07-11.
CFG = {"max_volume": 0.03, "office_names": ["office"], "allow_group_volume": False}


class TestVolumeCap(unittest.TestCase):
    def test_over_cap_refused(self):
        with self.assertRaises(safety.SafetyError):
            safety.set_volume(fake_cast(), 0.04, CFG)

    def test_way_over_cap_refused(self):
        with self.assertRaises(safety.SafetyError):
            safety.set_volume(fake_cast(), 1.0, CFG)

    def test_negative_refused(self):
        with self.assertRaises(safety.SafetyError):
            safety.set_volume(fake_cast(), -0.01, CFG)

    def test_at_cap_allowed(self):
        cast = fake_cast()
        safety.set_volume(cast, 0.03, CFG)
        cast.set_volume.assert_called_once_with(0.03)


class TestOfficeProtection(unittest.TestCase):
    def test_office_refused_any_level(self):
        for level in (0.0, 0.001, 0.03):
            with self.assertRaises(safety.SafetyError):
                safety.set_volume(fake_cast(name="Office"), level, CFG)

    def test_office_case_insensitive(self):
        with self.assertRaises(safety.SafetyError):
            safety.set_volume(fake_cast(name="  OFFICE "), 0.01, CFG)


class TestGroupRules(unittest.TestCase):
    def test_group_refused_by_default(self):
        with self.assertRaises(safety.SafetyError):
            safety.set_volume(fake_cast(name="Everywhere", cast_type="group"), 0.01, CFG)

    def test_group_with_office_member_refused_even_when_enabled(self):
        cfg = dict(CFG, allow_group_volume=True)
        devices = {"u-office": "Office", "u-kitchen": "Kitchen"}
        with mock.patch.object(safety, "resolve_group_members",
                               return_value=["u-office", "u-kitchen"]):
            with self.assertRaises(safety.SafetyError):
                safety.set_volume(fake_cast(name="Everywhere", cast_type="group"),
                                  0.01, cfg, devices)

    def test_group_unresolvable_members_refused(self):
        cfg = dict(CFG, allow_group_volume=True)
        with mock.patch.object(safety, "resolve_group_members", return_value=None):
            with self.assertRaises(safety.SafetyError):
                safety.set_volume(fake_cast(name="Everywhere", cast_type="group"),
                                  0.01, cfg, {})

    def test_group_clean_members_allowed_when_enabled(self):
        cfg = dict(CFG, allow_group_volume=True)
        devices = {"u-kitchen": "Kitchen", "u-living": "Living Room"}
        cast = fake_cast(name="Downstairs", cast_type="group")
        with mock.patch.object(safety, "resolve_group_members",
                               return_value=["u-kitchen", "u-living"]):
            safety.set_volume(cast, 0.02, cfg, devices)
        cast.set_volume.assert_called_once_with(0.02)


class TestSafeCastProxy(unittest.TestCase):
    def test_volume_methods_hidden(self):
        sc = safety.SafeCast(fake_cast())
        for blocked in ("set_volume", "volume_up", "volume_down"):
            with self.assertRaises(AttributeError):
                getattr(sc, blocked)

    def test_allowed_passthrough(self):
        cast = fake_cast(name="Kitchen")
        sc = safety.SafeCast(cast)
        self.assertEqual(sc.name, "Kitchen")

    def test_readonly(self):
        sc = safety.SafeCast(fake_cast())
        with self.assertRaises(AttributeError):
            sc.name = "hax"


if __name__ == "__main__":
    unittest.main()

"""config: output_mode migration + set_user_value (isolated temp dir).

Monkeypatches config_dir to a tempdir so the real user config is never touched.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from streamer import config


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = config.config_dir
        config.config_dir = lambda: Path(self.tmp)

    def tearDown(self):
        config.config_dir = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, d):
        (Path(self.tmp) / "config.json").write_text(json.dumps(d), encoding="utf-8")

    def _read(self):
        return json.loads((Path(self.tmp) / "config.json").read_text(encoding="utf-8"))

    # -- migration -----------------------------------------------------------

    def test_migration_true_to_speakers(self):
        self._write({"mute_local_while_casting": True})
        self.assertEqual(config.load()["output_mode"], "speakers")

    def test_migration_false_to_both(self):
        self._write({"mute_local_while_casting": False})
        self.assertEqual(config.load()["output_mode"], "both")

    def test_explicit_output_mode_wins_over_legacy(self):
        self._write({"mute_local_while_casting": False, "output_mode": "auto"})
        self.assertEqual(config.load()["output_mode"], "auto")

    def test_default_when_absent(self):
        self._write({"last_device": "X"})
        self.assertEqual(config.load()["output_mode"], "speakers")

    # -- set_user_value ------------------------------------------------------

    def test_set_user_value_roundtrips_and_preserves(self):
        self._write({"last_device": "Kitchen", "cast_mode": "auto",
                     "mirror_target_delay_ms": 400})
        config.set_user_value("output_mode", "auto")
        d = self._read()
        self.assertEqual(d["output_mode"], "auto")
        self.assertEqual(d["last_device"], "Kitchen")     # preserved
        self.assertEqual(d["cast_mode"], "auto")

    def test_set_user_value_survives_a_later_save(self):
        # save() persists only APP_OWNED_KEYS but must not drop a user-policy
        # key written by set_user_value (it re-reads on_disk under the lock).
        config.set_user_value("mirror_target_delay_ms", 100)
        config.save({"last_device": "Den", "stream_type": "LIVE",
                     "firewall_registered_image": None})
        d = self._read()
        self.assertEqual(d["mirror_target_delay_ms"], 100)
        self.assertEqual(d["last_device"], "Den")

    def test_set_user_value_then_load_reflects(self):
        config.set_user_value("output_mode", "this_pc")
        self.assertEqual(config.load()["output_mode"], "this_pc")


if __name__ == "__main__":
    unittest.main()

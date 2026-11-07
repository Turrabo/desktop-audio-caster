"""Self-update: release parsing, download verification, the install swap.

No network: the HTTP opener is a fake. The install test runs against real
temp files, including a target that stays locked for a while (the old exe
still running) - the case the copy-then-replace retry exists for.
"""
import hashlib
import io
import json
import re
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from streamer import __version__, updater

ROOT = Path(__file__).resolve().parents[1]
EXE = b"MZ" + b"\x00" * 1000


def release(tag, *, name=updater.ASSET_NAME, body=EXE, digest=True):
    asset = {"name": name, "size": len(body),
             "browser_download_url": "https://example.invalid/x.exe"}
    if digest:
        asset["digest"] = "sha256:" + hashlib.sha256(body).hexdigest()
    return {"tag_name": tag, "assets": [asset]}


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def opener_for(payload: bytes):
    return lambda url: FakeResp(payload)


class TestVersion(unittest.TestCase):
    def test_package_version_matches_exe_resource(self):
        # the exe's file version (version_info.txt) and the version the app
        # compares against must never drift apart
        text = (ROOT / "version_info.txt").read_text(encoding="utf-8")
        m = re.search(r"StringStruct\('ProductVersion', '([\d.]+)'\)", text)
        self.assertEqual(m.group(1), __version__)

    def test_parse(self):
        self.assertEqual(updater.parse_version("v1.3.1"), (1, 3, 1))
        self.assertEqual(updater.parse_version("1.10.0"), (1, 10, 0))
        self.assertIsNone(updater.parse_version("v1.4.0-beta"))
        self.assertIsNone(updater.parse_version(""))


class TestPickUpdate(unittest.TestCase):
    def test_newer_release_is_offered(self):
        u = updater.pick_update(release("v1.4.0"), current="1.3.1")
        self.assertEqual(u.version, "1.4.0")
        self.assertEqual(u.size, len(EXE))
        self.assertEqual(u.sha256, hashlib.sha256(EXE).hexdigest())

    def test_numeric_not_string_comparison(self):
        self.assertIsNotNone(updater.pick_update(release("v1.10.0"), current="1.9.0"))

    def test_same_or_older_is_not_offered(self):
        self.assertIsNone(updater.pick_update(release("v1.3.1"), current="1.3.1"))
        self.assertIsNone(updater.pick_update(release("v1.2.0"), current="1.3.1"))

    def test_release_without_exe_is_ignored(self):
        self.assertIsNone(updater.pick_update(release("v2.0.0", name="notes.txt"),
                                              current="1.3.1"))

    def test_missing_digest_is_allowed(self):
        u = updater.pick_update(release("v1.4.0", digest=False), current="1.3.1")
        self.assertIsNone(u.sha256)

    def test_check_reads_api(self):
        payload = json.dumps(release("v99.0.0")).encode()
        self.assertEqual(updater.check(opener_for(payload)).version, "99.0.0")


class TestDownload(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.dest = self.dir / updater.UPDATE_NAME

    def update(self, body=EXE, **kw):
        return updater.pick_update(release("v9.0.0", body=body, **kw), current="1.0.0")

    def test_good_download_lands(self):
        updater.download(self.update(), self.dest, opener_for(EXE))
        self.assertEqual(self.dest.read_bytes(), EXE)
        self.assertEqual(list(self.dir.iterdir()), [self.dest])   # no .part left

    def test_truncated_download_rejected(self):
        with self.assertRaisesRegex(ValueError, "size"):
            updater.download(self.update(), self.dest, opener_for(EXE[:-10]))
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_corrupt_download_rejected(self):
        bad = EXE[:-1] + b"\x01"                     # same size, wrong bytes
        with self.assertRaisesRegex(ValueError, "sha256"):
            updater.download(self.update(), self.dest, opener_for(bad))
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_non_executable_rejected(self):
        page = b"<html>" + b"x" * 995
        u = self.update(body=page)                    # digest matches the page
        with self.assertRaisesRegex(ValueError, "executable"):
            updater.download(u, self.dest, opener_for(page))
        self.assertFalse(self.dest.exists())


class TestInstall(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.source = self.dir / updater.UPDATE_NAME
        self.target = self.dir / updater.ASSET_NAME
        self.source.write_bytes(b"MZ new")
        self.target.write_bytes(b"MZ old")
        self.launched = []

    def test_replaces_and_launches(self):
        ok = updater.install(self.source, self.target, wait=1,
                             launch=self.launched.append)
        self.assertTrue(ok)
        self.assertEqual(self.target.read_bytes(), b"MZ new")
        self.assertEqual(self.launched, [self.target])
        self.assertFalse(self.target.with_name(self.target.name + ".tmp").exists())

    def test_waits_for_old_exe_to_let_go(self):
        # hold the target open without delete-sharing, as a running exe does,
        # then release it after 0.6 s
        locked = self._lock(self.target)
        threading.Timer(0.6, lambda: self._unlock(locked)).start()
        t0 = time.monotonic()
        ok = updater.install(self.source, self.target, wait=5,
                             launch=self.launched.append)
        self.assertTrue(ok)
        self.assertGreaterEqual(time.monotonic() - t0, 0.5)
        self.assertEqual(self.target.read_bytes(), b"MZ new")

    def test_gives_up_but_still_relaunches(self):
        locked = self._lock(self.target)
        self.addCleanup(self._unlock, locked)
        with self.assertLogs("streamer.updater", "ERROR"):
            ok = updater.install(self.source, self.target, wait=0.3,
                                 launch=self.launched.append)
        self.assertFalse(ok)
        self.assertEqual(self.launched, [self.target])     # never left without app
        self._unlock(locked)
        self.assertEqual(self.target.read_bytes(), b"MZ old")

    @staticmethod
    def _lock(path):
        """Open with no FILE_SHARE_DELETE, so os.replace onto it fails the way
        it does onto a running exe."""
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        h = k32.CreateFileW(str(path), 0x80000000, 0x1, None, 3, 0x80, None)
        assert h != wintypes.HANDLE(-1).value, ctypes.get_last_error()
        return h

    @staticmethod
    def _unlock(h):
        import ctypes
        if h:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))


class TestCleanupAndEntry(unittest.TestCase):
    def test_cleanup_removes_leftovers(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / updater.UPDATE_NAME).write_bytes(b"MZ")
        (d / (updater.UPDATE_NAME + ".part")).write_bytes(b"MZ")
        updater.cleanup(d, tries=20, gap=0.05)
        deadline = time.monotonic() + 2
        while any(d.iterdir()) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(list(d.iterdir()), [])

    def test_normal_launch_is_not_installer(self):
        self.assertFalse(updater.maybe_run_installer(["app.exe"]))
        self.assertFalse(updater.maybe_run_installer(["app.exe", "--other", "x"]))

    def test_from_source_has_no_self_update(self):
        self.assertIsNone(updater.frozen_exe())


class TestTrayUpdateClick(unittest.TestCase):
    """TrayApp._start_update glue, on a bare instance (no tray, no network)."""

    def make(self, casting=False):
        from streamer import tray
        app = tray.TrayApp.__new__(tray.TrayApp)
        app.notes, app.calls, app._updating = [], [], False
        app.checker = updater.UpdateChecker(on_found=None)
        app.checker.available = updater.Update("9.0.0", "u", 1, None)
        app.icon = type("I", (), {"update_menu": lambda self: None})()
        app.ctl = type("C", (), {"session": object() if casting else None,
                                 "busy": lambda self: False})()
        app._notify = app.notes.append
        app._exit = lambda: app.calls.append("exit")
        return app

    def patched(self, **fns):
        for name, fn in fns.items():
            orig = getattr(updater, name)
            setattr(updater, name, fn)
            self.addCleanup(setattr, updater, name, orig)

    def run_update(self, app):
        app._start_update()
        for t in threading.enumerate():
            if t.name == "update-apply":
                t.join(5)

    def test_refuses_while_casting(self):
        app = self.make(casting=True)
        self.patched(frozen_exe=lambda: Path("C:/x/DesktopAudioStreamer.exe"))
        self.run_update(app)
        self.assertEqual(app.calls, [])
        self.assertIn("Stop casting first", app.notes[0])

    def test_success_hands_over_then_exits(self):
        app = self.make()
        self.patched(frozen_exe=lambda: Path("C:/x/DesktopAudioStreamer.exe"),
                     download=lambda u, dest: app.calls.append("download"),
                     hand_over=lambda src, dst: app.calls.append(("hand_over", src.name)))
        self.run_update(app)
        self.assertEqual(app.calls, ["download", ("hand_over", updater.UPDATE_NAME), "exit"])

    def test_failure_keeps_running_and_says_so(self):
        app = self.make()

        def boom(u, dest):
            raise ValueError("download sha256 mismatch")
        self.patched(frozen_exe=lambda: Path(tempfile.gettempdir()) / "nope.exe",
                     download=boom)
        with self.assertLogs("streamer.tray", "ERROR"):
            self.run_update(app)
        self.assertEqual(app.calls, [])
        self.assertFalse(app._updating)               # can retry
        self.assertIn("sha256 mismatch", app.notes[0])

    def test_from_source_does_nothing(self):
        app = self.make()
        self.patched(frozen_exe=lambda: None)
        self.run_update(app)
        self.assertEqual((app.calls, app.notes), ([], []))


if __name__ == "__main__":
    unittest.main()

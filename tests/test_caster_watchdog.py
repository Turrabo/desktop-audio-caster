"""Watchdog bound: a session whose stream is never fetched (the firewall-block
signature) must land in a persistent ERROR after a bounded number of re-casts
and hand teardown to the controller - while a session that WAS fetched (even
if the client later drops) must keep retrying forever as before."""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from streamer import caster


def make_cast(player_state="IDLE"):
    cast = MagicMock()
    cast.name = "Everywhere"
    cast.socket_client.host = "127.0.0.1"
    cast.media_controller.status.player_state = player_state
    return cast


def make_session(fetch_count_fn, on_gave_up=None):
    capture = MagicMock()
    capture.healthy = True
    discovery = MagicMock()
    session = caster.CastSession(
        discovery, make_cast(), 8765, "LIVE", capture,
        fetch_count_fn=fetch_count_fn, on_gave_up=on_gave_up)
    # recover() swaps in a fresh cast via discovery; keep it in mock-land
    session._recover = lambda: session._play()
    return session


class TestNeverFetchedBound(unittest.TestCase):
    def setUp(self):
        self._saved = (caster.WATCHDOG_PERIOD, caster.NO_FETCH_GRACE_SECONDS,
                       caster.BACKOFF_START, caster.BACKOFF_CAP)
        caster.WATCHDOG_PERIOD = 0.05
        caster.NO_FETCH_GRACE_SECONDS = 0.15
        caster.BACKOFF_START = caster.BACKOFF_CAP = 0.01

    def tearDown(self):
        (caster.WATCHDOG_PERIOD, caster.NO_FETCH_GRACE_SECONDS,
         caster.BACKOFF_START, caster.BACKOFF_CAP) = self._saved

    def _run_until(self, session, states, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not predicate():
            time.sleep(0.05)

    def test_never_fetched_lands_in_error_and_stops(self):
        states, gave_up = [], []
        session = make_session(fetch_count_fn=lambda: 0,
                               on_gave_up=gave_up.append)
        session._on_state = lambda s, d=None: states.append((s, d))
        session.start()
        self._run_until(session, states,
                        lambda: any(s == "ERROR" for s, _ in states))
        error_states = [(s, d) for s, d in states if s == "ERROR"]
        self.assertTrue(error_states, f"no ERROR emitted; got {states}")
        self.assertIn("never fetched", error_states[-1][1])
        self.assertIn("Firewall", error_states[-1][1])
        # teardown must be handed to the controller, and the watchdog must
        # EXIT (no more retries) once it gives up
        self.assertTrue(gave_up, "on_gave_up was not called")
        session._thread.join(timeout=2)
        self.assertFalse(session._thread.is_alive(), "watchdog kept running")

    def test_fetched_then_lost_keeps_retrying_forever(self):
        # One GET was served early, then the client vanished: NOT the
        # firewall signature - the retry loop must never trip the bound,
        # even though the live client count is back to zero.
        states, gave_up = [], []
        session = make_session(fetch_count_fn=lambda: 1,   # monotonic total
                               on_gave_up=gave_up.append)
        session._on_state = lambda s, d=None: states.append((s, d))
        session.start()
        time.sleep(1.0)   # dozens of watchdog cycles at the shrunk period
        try:
            self.assertFalse(gave_up, "gave up despite an earlier fetch")
            self.assertFalse(
                any(s == "ERROR" for s, _ in states),
                f"spurious ERROR: {states}")
            self.assertTrue(session._thread.is_alive(),
                            "watchdog exited; should retry forever")
        finally:
            session._stop.set()
            session._thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

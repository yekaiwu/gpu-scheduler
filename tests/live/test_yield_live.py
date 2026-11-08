"""Live yield_apps check (QUESTIONS #41) against a harmless system app: Calculator is opened in the background, quit by
the real daemon through a reservation window that is active now, then reopened when the window no longer applies.
Skipped if Calculator is already running (the test must not close something the user opened)."""

import datetime as dt
import subprocess
import time

import pytest

from gsched.config import Config

pytestmark = pytest.mark.live
APP = "/System/Applications/Calculator.app"
EXE = APP + "/Contents/MacOS/Calculator"


def _running() -> bool:
    return subprocess.run(["/usr/bin/pgrep", "-f", EXE], capture_output=True).returncode == 0


def _until(d, cond, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        d.step(0.2)
        if cond():
            return True
    return False


def test_live_reservation_quits_and_reopens_calculator(live_daemon):
    if _running():
        pytest.skip("Calculator is already open; not touching the user's app")
    subprocess.run(["/usr/bin/open", "-g", "-j", "-a", APP], check=True)
    try:
        assert _wait(_running, 15)
        now = dt.datetime.now().astimezone()
        start = (now - dt.timedelta(minutes=1)).strftime("%H:%M")
        end = (now + dt.timedelta(minutes=30)).strftime("%H:%M")
        r = {"name": "live-window", "days": ["daily"], "start": start, "end": end, "project": "owner",
             "memory_gib": 1}
        a = {"name": "calc", "app": APP, "reservations": ["live-window"], "quit": "sigterm", "reopen_delay_s": 0}
        d = live_daemon(Config.defaults().replace(enforce=True, night_window=None, reservations=(r,),
                                                  yield_apps=(a,)))
        assert _until(d, lambda: not _running(), 20), "the daemon did not quit Calculator"
        assert _until(d, lambda: d.yield_apps.state["calc"].phase == "closed", 5)
        kinds = [e["kind"] for e in d.store.events(since_ms=0) if e["kind"].startswith("yield_app")]
        assert kinds[:2] == ["yield_app_quit", "yield_app_closed"]
        # the need ends (the entry no longer names the window): the daemon reopens it in the background
        d.cfg = d.cfg.replace(yield_apps=({**a, "reservations": [], "for_jobs_gib": 1000},))
        assert _until(d, _running, 20), "the daemon did not reopen Calculator"
        assert _until(d, lambda: d.yield_apps.state["calc"].phase == "idle", 10)
        assert "yield_app_reopened" in [e["kind"] for e in d.store.events(since_ms=0)]
        assert not [e for e in d.store.events(since_ms=0) if e["kind"] == "tick_error"]
    finally:
        if _running():                 # no AppleScript here: it would raise an Automation consent prompt
            subprocess.run(["/usr/bin/pkill", "-TERM", "-f", EXE], capture_output=True)


def _wait(cond, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.2)
    return False

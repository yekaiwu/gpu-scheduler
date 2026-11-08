from __future__ import annotations

import json
import os
import sys
import time

import pytest

from gsched.clock import SystemClock
from gsched.config import Config


def drive(d, until, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        d.step(0.05)
        r = until()
        if r:
            return r
    raise AssertionError("timed out waiting for condition")


@pytest.fixture
def live_cfg():
    return Config.defaults().replace(enforce=True, timezone="UTC", night_window=None, prevent_sleep=False)


@pytest.fixture
def live_daemon(short_tmp, live_cfg):
    from gsched.daemon import Daemon
    from gsched.platform.darwin import DarwinPlatform
    made = []

    def make(cfg=None):
        d = Daemon(short_tmp, cfg=cfg or live_cfg, clock=SystemClock(), platform=DarwinPlatform(),
                   log_enabled=True, tcc_home=str(short_tmp / "nohome"))   # never probe the real ~/Documents
        d.start(listen=True)
        made.append(d)
        return d

    make.home = short_tmp
    yield make
    for d in made:
        try:
            d.stop()
        except Exception:
            pass


def add_project(d, name="p", **kw):
    with open(os.path.join(d.paths.projects, f"{name}.json"), "w") as fh:
        json.dump({"name": name, **kw}, fh)
    d.reload_all()


def live_spec(command, **over):
    s = {"schema": "gpu-scheduler.job.v1", "project": "p", "command": command, "cwd": "/tmp",
         "resources": {"memory_gib": 0.25, "gpu": "none"}, "runtime": {"estimate_s": 60}, "checkpoint": "atomic"}
    for k, v in over.items():
        if isinstance(v, dict) and k in ("resources", "runtime"):
            s[k] = {**s[k], **v}
        else:
            s[k] = v
    return s


PY = sys.executable

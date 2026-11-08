from __future__ import annotations

import os
import threading
import time

import pytest

from gsched.clock import FakeClock
from gsched.config import Config
from gsched.daemon import Daemon
from gsched.platform.fake import FakePlatform
from tests.conftest import T0_MS


class ThreadedDaemon:
    """A real Daemon driven by ONE dedicated thread (it creates and owns every daemon object). The test thread
    only talks to it through the socket, so MCP/CLI processes can block on it like a real client."""

    def __init__(self, home, cfg=None):
        self.home = home
        self.clock = FakeClock(T0_MS)
        self.plat = FakePlatform(clock=self.clock)
        self.cfg = cfg or Config.defaults().replace(enforce=True, timezone="UTC")
        self.ready = threading.Event()
        self.stop_flag = False
        self.error = None
        self.d = None
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()
        assert self.ready.wait(10), self.error

    def _run(self):
        try:
            self.d = Daemon(self.home, cfg=self.cfg, clock=self.clock, platform=self.plat, log_enabled=False)
            self.d.start()
            for name, kw in (("p", {"mcp_submit": True, "mcp_terminate": False, "default_cwd": "/tmp"}), ("q", {})):
                import json
                with open(os.path.join(self.d.paths.projects, f"{name}.json"), "w") as fh:
                    json.dump({"name": name, **kw}, fh)
            self.d.reload_all()
            self.ready.set()
            while not self.stop_flag:
                self.d.step(0.02)
                self.clock.advance(0.02)
        except Exception as e:             # pragma: no cover
            self.error = e
            self.ready.set()
        finally:
            if self.d is not None:
                self.d.stop()

    @property
    def sock(self):
        return str(self.d.socket_path)

    def stop(self):
        self.stop_flag = True
        self.t.join(10)


@pytest.fixture
def tdaemon(short_tmp, monkeypatch):
    t = ThreadedDaemon(short_tmp)
    monkeypatch.setenv("GSCHED_SOCKET", t.sock)
    monkeypatch.setenv("GSCHED_HOME", str(short_tmp))
    yield t
    t.stop()

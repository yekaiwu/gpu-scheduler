from __future__ import annotations

import json
import os
import socket
import threading

import pytest


class FakeDaemon:
    """A scripted daemon socket for CLI tests (a thread is fine here: the one-thread rule is about the daemon)."""

    def __init__(self, path):
        self.path = str(path)
        self.requests: list[dict] = []
        self.script: dict = {}
        self.delay: dict = {}
        self.srv = socket.socket(socket.AF_UNIX)
        self.srv.bind(self.path)
        self.srv.listen(8)
        self.stop = False
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def reply(self, method, result=None, error=None, ok=None):
        self.script[method] = ({"rpc": "gpu-scheduler.rpc.v1", "ok": True, "result": result} if error is None
                               else {"rpc": "gpu-scheduler.rpc.v1", "ok": False, "error": error})

    def _loop(self):
        self.srv.settimeout(0.1)
        while not self.stop:
            try:
                c, _ = self.srv.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        buf = b""
        while b"\n" not in buf:
            chunk = c.recv(65536)
            if not chunk:
                return
            buf += chunk
        req = json.loads(buf.split(b"\n")[0])
        self.requests.append(req)
        resp = self.script.get(req["method"])
        if callable(resp):
            resp = resp(req)
        if resp is None:
            resp = {"rpc": "gpu-scheduler.rpc.v1", "ok": False, "error": {"code": "invalid", "message": "unscripted"}}
        ev = self.delay.get(req["method"])
        if ev:
            ev.wait(5)
        try:
            c.sendall(json.dumps(resp).encode() + b"\n")
        except OSError:
            pass
        c.close()

    def close(self):
        self.stop = True
        self.srv.close()


@pytest.fixture
def fake(short_tmp, monkeypatch):
    f = FakeDaemon(short_tmp / "s.sock")
    monkeypatch.setenv("GSCHED_HOME", str(short_tmp / "home"))
    monkeypatch.setenv("GSCHED_SOCKET", f.path)
    yield f
    f.close()


@pytest.fixture
def cli(capsys):
    from gsched import cli as c

    def run(*argv):
        rc = c.main(list(argv))
        out = capsys.readouterr()
        return rc, out.out, out.err
    return run

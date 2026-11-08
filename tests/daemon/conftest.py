from __future__ import annotations

import json
import socket

import pytest

from gsched.config import Config
from gsched.daemon import Daemon
from tests.helpers import spec_dict


class Pending:
    """A request whose response may arrive later (parked waits). No threads: poll() steps the daemon."""

    def __init__(self, d, method, door="cli", **params):
        self.d = d
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(d.socket_path))
        req = {"rpc": "gpu-scheduler.rpc.v1", "method": method, "params": params, "door": door}
        self.sock.sendall(json.dumps(req).encode() + b"\n")
        self.sock.setblocking(False)
        self.buf = b""
        self.resp = None

    def poll(self, steps=1):
        for _ in range(steps):
            if self.resp is not None:
                return self.resp
            self.d.step(0)
            try:
                chunk = self.sock.recv(65536)
            except BlockingIOError:
                continue
            if chunk == b"":
                self.resp = {"closed": True}
            else:
                self.buf += chunk
                if b"\n" in self.buf:
                    self.resp = json.loads(self.buf.split(b"\n")[0])
        return self.resp

    def close(self):
        self.sock.close()


def client(d, method, door="cli", raw=False, **params):
    p = Pending(d, method, door, **params)
    for _ in range(1000):
        r = p.poll()
        if r is not None:
            break
    else:
        raise AssertionError(f"no response to {method}")
    p.close()
    if raw:
        return r
    if not r.get("ok"):
        raise ClientError(r["error"])
    return r["result"]


class ClientError(Exception):
    def __init__(self, err):
        super().__init__(err["message"])
        self.code = err["code"]
        self.err = err


def run_ticks(d, clock, seconds, step=1.0):
    n = int(round(seconds / step))
    for _ in range(n):
        clock.advance(step)
        d.step(0)


def project(d, name, weight=1, **kw):
    import os
    body = {"name": name, "weight": weight, **kw}
    with open(os.path.join(d.paths.projects, f"{name}.json"), "w") as fh:
        json.dump(body, fh)
    d.reload_all()


def spec(**over):
    d = spec_dict(**over)
    d.setdefault("cwd", "/tmp")
    return d


@pytest.fixture
def cfg():
    return Config.defaults().replace(enforce=True, timezone="UTC")


@pytest.fixture
def daemon(short_tmp, clock, plat, cfg):
    d = Daemon(short_tmp, cfg=cfg, clock=clock, platform=plat, log_enabled=False, debug=True)
    d.start(listen=True)
    project(d, "p")
    project(d, "q")
    yield d
    d.stop()


@pytest.fixture
def make_daemon(short_tmp, clock, plat):
    made = []

    def make(cfg=None, log_enabled=False, **kw):
        c = cfg or Config.defaults().replace(enforce=True, timezone="UTC")
        d = Daemon(short_tmp, cfg=c, clock=clock, platform=plat, log_enabled=log_enabled, debug=True, **kw)
        d.start(listen=True)
        made.append(d)
        return d
    yield make
    for d in made:
        try:
            d.stop()
        except Exception:
            pass

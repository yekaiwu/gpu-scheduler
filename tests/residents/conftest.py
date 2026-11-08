from __future__ import annotations

import json
import os
import socket

import pytest

from tests.daemon.conftest import *  # noqa: F401,F403  (fixtures: daemon, make_daemon, cfg, helpers)


class MiniHttp:
    """A single-threaded loopback HTTP server pumped by the test between daemon steps. No threads."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.srv.setblocking(False)
        self.port = self.srv.getsockname()[1]
        self.routes: dict[str, tuple[int, str]] = {}
        self.hang = False
        self.requests: list[str] = []
        self.clients: list[socket.socket] = []

    def route(self, path, status, body=""):
        self.routes[path] = (status, body)

    def pump(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except BlockingIOError:
                break
            c.setblocking(False)
            self.clients.append(c)
        for c in list(self.clients):
            try:
                data = c.recv(65536)
            except BlockingIOError:
                continue
            except OSError:
                data = b""
            if not data:
                self.clients.remove(c)
                continue
            req = data.decode("latin1")
            self.requests.append(req)
            if self.hang:
                continue
            path = req.split(" ")[1]
            status, body = self.routes.get(path, (404, ""))
            c.sendall(f"HTTP/1.0 {status} X\r\nContent-Length: {len(body)}\r\n\r\n{body}".encode())
            c.close()
            self.clients.remove(c)

    def close(self):
        for c in self.clients:
            c.close()
        self.srv.close()


@pytest.fixture
def http():
    h = MiniHttp()
    yield h
    h.close()


def write_profile(d, name="llama", project="local-llm", reload=True, **over):
    body = {"name": name, "project": project,
            "spec": {"command": ["sleep", "100"], "cwd": "/tmp",
                     "resources": {"memory_gib": 10, "gpu": "exclusive", "cpu_cores": 1}, "priority": 10}}
    body.update(over)
    with open(os.path.join(d.paths.profiles, f"{name}.json"), "w") as fh:
        json.dump(body, fh)
    if reload:
        d.reload_all()
    return body


def pump_steps(d, clock, http, seconds, advance=0.25):
    """Advance `seconds` of fake time in small slices so 1 s probe timeouts are not hit by the pacing."""
    for _ in range(int(round(seconds / advance))):
        clock.advance(advance)
        http.pump()
        d.step(0)
        http.pump()
        d.step(0)


@pytest.fixture
def rd(daemon):
    project(daemon, "local-llm")
    return daemon

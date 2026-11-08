"""The only socket client code. Used by the CLI, the MCP adapter, the dashboard and the launcher helper (09 section 5)."""

from __future__ import annotations

import errno
import json
import os
import socket
import time

from gsched.paths import Paths

RPC = "gpu-scheduler.rpc.v1"
MAX_LINE = 4 * 1024 * 1024


class ConnectError(Exception):
    """Cannot reach the daemon (CLI exit 3)."""

    def __init__(self, path: str, cause: Exception | None = None):
        super().__init__(f"gsched daemon is not running (socket: {path}). "
                         "Start it with `gsched install` or `gsched daemon`.")
        self.path = path
        self.cause = cause


class SocketBlocked(ConnectionError):
    """connect() to the socket was refused with EPERM/EACCES: almost always an agent's command sandbox (Codex,
    sandboxed shells) that forbids Unix sockets. Not retried: waiting does not change a sandbox policy."""

    def __init__(self, path: str, cause: Exception | None = None):
        super().__init__(f"cannot connect to the gsched socket {path}: {cause} -- this process is probably inside "
                         "an agent sandbox that blocks Unix sockets. Run gsched outside the sandbox (approve the "
                         "command with escalated permissions) or use the gpu-scheduler MCP tools instead.")
        self.path = path
        self.cause = cause


class ProtocolError(Exception):
    pass


def socket_path(home=None, socket_override: str | None = None) -> str:
    if socket_override:
        return socket_override
    env = os.environ.get("GSCHED_SOCKET")
    if env:
        return env
    paths = Paths.resolve(home)
    try:
        from gsched.config import Config
        cfg = Config.load(paths.config)
        return paths.socket(cfg.socket_path)
    except Exception:
        return paths.default_socket


def _exchange(path: str, request: dict, timeout: float | None) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(10.0 if timeout is None else min(timeout, 10.0))
        try:
            s.connect(path)
        except (FileNotFoundError, ConnectionRefusedError, NotADirectoryError) as e:
            raise ConnectError(path, e)
        except OSError as e:
            if e.errno in (errno.ENOENT, errno.ECONNREFUSED, errno.ENOTDIR):
                raise ConnectError(path, e)
            if e.errno in (errno.EPERM, errno.EACCES):
                raise SocketBlocked(path, e)
            raise
        s.settimeout(timeout)
        s.sendall(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        buf = bytearray()
        while b"\n" not in buf:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                raise
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_LINE:
                raise ProtocolError("response too large")
        if not buf:
            raise ProtocolError("daemon closed the connection without a response")
        line = bytes(buf).split(b"\n", 1)[0]
        return json.loads(line)
    finally:
        s.close()


def call(method: str, params: dict | None = None, *, door: str = "cli", timeout: float | None = 10.0,
         home=None, socket_override: str | None = None) -> dict:
    """Connect, send one JSON line, read one JSON line. Returns the envelope.
    Raises ConnectError (exit 3). timeout=None blocks (wait / get --wait)."""
    path = socket_path(home, socket_override)
    req = {"rpc": RPC, "method": method, "params": params or {}, "door": door}
    return _exchange(path, req, timeout)


def call_retry(method: str, params: dict | None = None, *, door: str = "cli", timeout: float | None = None,
               retry_s: float = 30.0, home=None, socket_override: str | None = None) -> dict:
    """Like call(), but retries ECONNREFUSED/ENOENT for up to `retry_s` (daemon restart, 07 section 6)."""
    deadline = time.monotonic() + retry_s
    while True:
        try:
            return call(method, params, door=door, timeout=timeout, home=home, socket_override=socket_override)
        except (ConnectError, ProtocolError, ConnectionResetError, BrokenPipeError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def result_or_raise(env: dict) -> dict:
    """Return env['result'] or raise ResponseError."""
    if env.get("ok"):
        return env.get("result", {})
    raise ResponseError(env.get("error", {}))


class ResponseError(Exception):
    def __init__(self, err: dict):
        super().__init__(err.get("message", "error"))
        self.code = err.get("code", "internal")
        self.message = err.get("message", "")
        self.detail = err.get("detail", {})
        self.err = err

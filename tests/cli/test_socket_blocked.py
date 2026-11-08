"""A sandbox that forbids Unix sockets (Codex's seatbelt, live 2026-11-08) gets a clear error, exit 3, no retry."""

import errno
import socket

import pytest

from gsched import client
from gsched.cli import main


@pytest.fixture
def blocked(monkeypatch, short_tmp):
    def connect(self, addr):
        raise PermissionError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setenv("GSCHED_SOCKET", str(short_tmp / "gsched.sock"))


def test_client_raises_socket_blocked(blocked):
    with pytest.raises(client.SocketBlocked, match="agent sandbox"):
        client.call("ping")


def test_cli_status_exits_3_with_hint(blocked, capsys):
    assert main(["status", "--brief"]) == 3
    err = capsys.readouterr().err
    assert "agent sandbox" in err and "Traceback" not in err


def test_exec_fail_closed_gives_up_at_once(blocked, capsys, monkeypatch):
    import time
    t0 = time.monotonic()
    rc = main(["exec", "--project", "p", "--mem", "0.1", "--gpu", "none", "--checkpoint", "atomic", "--est", "1m",
               "--fail-closed", "--", "/usr/bin/true"])
    assert rc == 75 and time.monotonic() - t0 < 5          # not the 20 s connect grace
    assert "agent sandbox" in capsys.readouterr().err

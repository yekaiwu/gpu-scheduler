import json
import os

from gsched.errors import GschedError
from gsched.projects import ProjectRegistry
import pytest


def test_registry_loads_and_rereads(tmp_path):
    warns = []
    reg = ProjectRegistry(str(tmp_path), lambda k, b: warns.append((k, b)))
    (tmp_path / "a.json").write_text(json.dumps({"name": "a", "weight": 3, "mcp_submit": True, "extra": 1}))
    (tmp_path / "bad.json").write_text(json.dumps({"name": "other"}))
    assert reg.reload_if_changed()
    assert reg.get("a").weight == 3 and reg.get("a").mcp_submit
    assert [k for k, _ in warns] == ["config_unknown_key", "config_reload_failed"]
    assert not reg.reload_if_changed()
    (tmp_path / "b.json").write_text(json.dumps({"name": "b"}))
    assert reg.reload_if_changed() and reg.names() == ["a", "b"]
    with pytest.raises(GschedError) as e:
        reg.get("zzz")
    assert e.value.code == "unknown_project"
    # unknown key logged once
    reg.reload_if_changed(force=True)
    assert [k for k, _ in warns].count("config_unknown_key") == 1


def test_command_allow_and_default_cwd(tmp_path):
    reg = ProjectRegistry(str(tmp_path))
    (tmp_path / "a.json").write_text(json.dumps({"name": "a", "command_allow": ["/usr/bin/"], "default_cwd": "/tmp"}))
    reg.reload_if_changed()
    p = reg.get("a")
    assert p.command_allow == ("/usr/bin/",) and p.default_cwd == "/tmp"


def test_resolve_command_tcc_eperm_gives_fda_hint(monkeypatch):
    """Review regression: under launchd without Full Disk Access, stat() of a command under ~/Documents fails with
    EPERM. os.path.isfile() swallowed that, so the user saw command_not_found instead of the TCC hint."""
    import os
    import pytest
    from gsched.checks import resolve_command
    from gsched.errors import GschedError
    real = os.stat

    def fake_stat(p, *a, **k):
        if str(p).startswith("/Users/me/Documents/"):
            raise PermissionError(1, "Operation not permitted", p)
        return real(p, *a, **k)
    monkeypatch.setattr(os, "stat", fake_stat)
    with pytest.raises(GschedError) as e:
        resolve_command(("/Users/me/Documents/proj/run.sh",), "/usr/bin:/bin")
    assert e.value.code == "cwd_not_accessible" and "Full Disk Access" in e.value.detail["hint"]
    with pytest.raises(GschedError) as e:
        resolve_command(("/nonexistent/run.sh",), "/usr/bin:/bin")
    assert e.value.code == "command_not_found"
    assert resolve_command(("/bin/sh",), "/usr/bin:/bin") == "/bin/sh"

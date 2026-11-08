import json
import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from gsched import cli as gcli
from gsched import templates
from gsched.install import render_plist

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def inst(short_tmp, monkeypatch, capsys):
    monkeypatch.delenv("GSCHED_SOCKET", raising=False)
    home = Path(os.path.realpath(short_tmp)) / "home"
    pdir = Path(os.path.realpath(short_tmp)) / "agents"

    def run(*extra, python=None):
        rc = gcli.main(["--home", str(home), "install", "--python", python or sys.executable, "--no-load",
                        "--plist-dir", str(pdir), *extra])
        out = capsys.readouterr()
        return rc, out.out, out.err
    run.home, run.pdir = home, pdir
    return run


def test_install_refuses_python39(inst, short_tmp):
    fake = short_tmp / "py39"
    fake.write_text("#!/bin/sh\necho '(3, 9) arm64'\n")
    fake.chmod(0o755)
    rc, out, err = inst(python=str(fake))
    assert rc != 0 and "3.9" in err and "3.12" in err
    assert not (inst.home / "venv").exists() and not inst.pdir.exists()
    fake.write_text("#!/bin/sh\necho '(3, 12) x86_64'\n")
    rc, out, err = inst(python=str(fake))
    assert rc != 0 and "arm64" in err


def test_install_end_to_end_and_copies_not_symlink(inst):
    rc, out, err = inst()
    assert rc == 0, err
    home = inst.home
    app = home / "app" / "gsched"
    assert app.is_dir() and not app.is_symlink()
    for p in (home / "app").rglob("*"):
        assert not p.is_symlink(), p
    assert (app / "dashboard_page.html").exists() and (app / "shim.py").exists()
    pth = list((home / "venv").rglob("gsched-app.pth"))
    assert len(pth) == 1 and pth[0].read_text().strip() == str(home / "app")
    wrapper = home / "bin" / "gsched"
    assert stat.S_IMODE(wrapper.stat().st_mode) == 0o755
    text = wrapper.read_text()
    assert str(home / "venv" / "bin" / "python3") in text and " -I -m gsched" in text and "{{" not in text
    # the wrapper really runs the installed copy, with -I
    r = subprocess.run([str(wrapper), "version"], capture_output=True, text=True, env={**os.environ, "GSCHED_HOME": str(home)})
    assert r.returncode == 0 and '"version":"0.1.0"' in r.stdout.replace(" ", "")
    r = subprocess.run([str(home / "venv" / "bin" / "python3"), "-I", "-c", "import gsched, sys; print(gsched.__file__)"],
                       capture_output=True, text=True, cwd="/tmp")
    assert r.stdout.strip() == str(home / "app" / "gsched" / "__init__.py")
    cfg = json.loads((home / "config.json").read_text())
    assert cfg["enforce"] is False and cfg["default_project"] == "default" and "_comment" in cfg
    assert json.loads((home / "projects" / "default.json").read_text()) == {"name": "default", "weight": 1}
    assert stat.S_IMODE((home / "config.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((home / "run").stat().st_mode) == 0o700
    plist = plistlib.loads((inst.pdir / "com.gpuscheduler.agent.plist").read_bytes())
    assert plist["Label"] == "com.gpuscheduler.agent"
    assert plist["ProgramArguments"] == [str(home / "venv" / "bin" / "python3"), "-I", "-m", "gsched", "daemon"]
    assert plist["EnvironmentVariables"] == {"GSCHED_HOME": str(home)} and plist["ProcessType"] == "Standard"
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert "PATH" in err and "Full Disk Access" in err and "enforce true" in err
    summary = json.loads(out.strip().splitlines()[-1])
    assert summary["loaded"] is False


def test_install_idempotent(inst):
    assert inst()[0] == 0
    before_plist = (inst.pdir / "com.gpuscheduler.agent.plist").read_text()
    rc, out, err = inst()
    assert rc == 0
    assert (inst.pdir / "com.gpuscheduler.agent.plist").read_text() == before_plist
    assert not list((inst.home / "rollback").glob("*"))             # unchanged plist: no backup
    assert not (inst.home / "app.new").exists() and not (inst.home / "app.old").exists()


def test_install_never_overwrites_config(inst):
    inst.home.mkdir(parents=True)
    (inst.home / "config.json").write_text('{"enforce": true, "grace_s": 33}')
    assert inst()[0] == 0
    assert json.loads((inst.home / "config.json").read_text()) == {"enforce": True, "grace_s": 33}
    assert not (inst.home / "projects" / "default.json").exists()          # only seeded with a brand-new config


def test_install_backs_up_a_different_plist(inst):
    inst.pdir.mkdir(parents=True)
    old = inst.pdir / "com.gpuscheduler.agent.plist"
    old.write_text("<plist>old</plist>")
    assert inst()[0] == 0
    backups = list((inst.home / "rollback").glob("com.gpuscheduler.agent.plist.*"))
    assert len(backups) == 1 and backups[0].read_text() == "<plist>old</plist>"


def test_install_with_dashboard_plist(inst):
    assert inst("--dashboard", "--dashboard-port", "9123")[0] == 0
    dash = plistlib.loads((inst.pdir / "com.gpuscheduler.agent.dashboard.plist").read_bytes())
    assert dash["Label"] == "com.gpuscheduler.agent.dashboard"
    assert dash["ProgramArguments"][1:] == ["-I", "-m", "gsched", "dashboard", "--port", "9123"]
    assert dash["KeepAlive"] is True and dash["ProcessType"] == "Background"
    assert dash["EnvironmentVariables"]["GSCHED_HOME"] == str(inst.home)


def test_uninstall_removes_plists_and_code_but_keeps_data(inst, capsys):
    inst("--dashboard")
    rc = gcli.main(["--home", str(inst.home), "uninstall", "--no-load", "--plist-dir", str(inst.pdir)])
    capsys.readouterr()
    assert rc == 0
    assert not list(inst.pdir.glob("*.plist"))
    assert not (inst.home / "venv").exists() and not (inst.home / "app").exists()
    assert (inst.home / "state").exists() and (inst.home / "config.json").exists()


def test_plist_template_has_no_personal_paths():
    for t in (ROOT / "deploy").glob("*.template"):
        txt = t.read_text()
        assert "/" + "Users/" not in txt and "{{LABEL}}" in txt, t
    assert "/" + "Users/" not in templates.AGENT_PLIST + templates.DASHBOARD_PLIST + templates.WRAPPER


def test_embedded_templates_match_deploy_files():
    assert templates.AGENT_PLIST == (ROOT / "deploy" / "com.gpuscheduler.agent.plist.template").read_text()
    assert templates.DASHBOARD_PLIST == (ROOT / "deploy" / "com.gpuscheduler.dashboard.plist.template").read_text()


def test_plist_renders_valid_plist():
    text = render_plist(templates.AGENT_PLIST, LABEL="com.example.a&b", VENV_PYTHON="/p y/<bin>/python3", HOME_DIR="/h&h")
    d = plistlib.loads(text.encode())
    assert d["Label"] == "com.example.a&b" and d["ProgramArguments"][0] == "/p y/<bin>/python3"
    assert d["StandardOutPath"] == "/h&h/logs/launchd.out.log"
    assert "{{" not in text
    text = render_plist(templates.DASHBOARD_PLIST, LABEL="x.dashboard", VENV_PYTHON="/v/python3", HOME_DIR="/h", PORT=8765)
    assert plistlib.loads(text.encode())["ProgramArguments"][-1] == "8765"
    with pytest.raises(Exception):
        render_plist("<plist><dict>{{LABEL}}</plist>", LABEL="x")


def test_default_home(monkeypatch, tmp_path):
    from gsched.paths import Paths, resolve_home
    monkeypatch.delenv("GSCHED_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_home() == os.path.realpath(str(tmp_path / ".local" / "gpu-scheduler"))
    monkeypatch.setenv("GSCHED_HOME", "~/elsewhere")
    assert resolve_home() == os.path.realpath(str(tmp_path / "elsewhere"))
    assert Paths.resolve("/tmp/x").db.endswith("/state/gsched.sqlite")


def test_install_venv_symlinks_flag(inst):
    """--venv-symlinks: the venv python is a symlink chain to the given interpreter (the same binary runs the daemon)."""
    rc, out, err = inst("--venv-symlinks")
    assert rc == 0, err
    vp = inst.home / "venv" / "bin" / "python3"
    assert vp.is_symlink()
    assert os.path.realpath(vp) == os.path.realpath(sys.executable)
    info = json.loads(out.strip().splitlines()[-1])
    assert info["venv_mode"] == "symlinks" and info["venv_python_realpath"] == os.path.realpath(sys.executable)
    assert "Full Disk Access must be granted to" in err and os.path.realpath(sys.executable) in err
    # the wrapper runs through it
    r = subprocess.run([str(inst.home / "bin" / "gsched"), "version"], capture_output=True, text=True,
                       env={**os.environ, "GSCHED_HOME": str(inst.home)})
    assert r.returncode == 0
    # switching back to the default rebuilds the venv with copies
    rc, out, err = inst()
    assert rc == 0, err
    assert not (inst.home / "venv" / "bin" / "python3").is_symlink()
    assert json.loads(out.strip().splitlines()[-1])["venv_mode"] == "copies"

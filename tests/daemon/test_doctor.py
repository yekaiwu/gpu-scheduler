import json

import pytest

from gsched import doctor
from tests.daemon.conftest import client, project, run_ticks, spec


def by(checks, cid):
    return [c for c in checks if c["id"] == cid]


def test_doctor_reports_tcc_warning(daemon, plat, clock, tmp_path):
    """The daemon's TCC view comes from probe subprocesses, never from an in-process listdir (a listdir of a protected
    folder blocks forever under launchd)."""
    seen = []

    def fake_listdir(path):
        seen.append(path)
        return []

    home = daemon.tcc.home
    plat.tcc[home + "/Documents"] = "denied"
    plat.tcc[home + "/Desktop"] = "absent"
    for r in daemon.tcc.roots():
        daemon.tcc.entries.pop(r, None)
    daemon.tcc.start_all()
    checks = doctor.daemon_checks(daemon, listdir=fake_listdir, home="/home/someone")
    tcc = by(checks, 7)
    docs = [c for c in tcc if "Documents" in c["name"] and "Mobile" not in c["name"]][0]
    assert docs["level"] == "WARN" and "Full Disk Access" in docs["fix"] and "launchctl kickstart" in docs["fix"]
    import os, sys
    assert os.path.realpath(sys.executable) in docs["fix"]
    assert [c for c in tcc if "Downloads" in c["name"]][0]["level"] == "PASS"
    assert [c for c in tcc if "Desktop" in c["name"]][0]["level"] == "PASS"
    assert not any(p.startswith(home) for p in seen)          # nothing under a protected root was listed in-process


def test_doctor_daemon_checks_shape(daemon, clock, plat):
    project(daemon, "p", default_cwd="/tmp")
    daemon.sync_projects()
    checks = doctor.daemon_checks(daemon, home="/nonexistent-home")
    ids = {c["id"] for c in checks}
    assert {4, 5, 6, 7, 8, 10, 11, 12, 13, 15, 16} <= ids
    assert by(checks, 4)[0]["level"] == "PASS"
    assert by(checks, 13)[0]["level"] == "INFO"
    assert "budget" in by(checks, 11)[0]["message"]
    for c in checks:
        assert c["level"] in ("PASS", "WARN", "FAIL", "INFO") and set(c) >= {"id", "name", "level", "message", "fix", "where"}


def test_doctor_rpc_and_profile_failures(daemon):
    import os
    with open(os.path.join(daemon.paths.profiles, "broken.json"), "w") as fh:
        json.dump({"name": "broken", "project": "nope", "spec": {}}, fh)
    daemon.reload_all()
    res = client(daemon, "doctor")["checks"]
    assert [c for c in res if c["id"] == 9 and c["level"] == "FAIL" and c["name"] == "profile broken"]


def test_doctor_warns_when_jobs_exceed_wired_limit(daemon):
    daemon.store.upsert_project("p", 1)
    from tests.helpers import make_spec
    daemon.store.insert_job(make_spec(resources={"memory_gib": 38, "gpu": "exclusive"}, cwd="/tmp"), queue="day",
                            state="queued", door="cli", now_ms=daemon.clock.now_ms())
    c = by(doctor.daemon_checks(daemon, home="/nonexistent"), 10)[0]
    assert c["level"] == "WARN" and "75%" in c["message"]


def test_doctor_foreign_heavy_listed(daemon, clock, plat):
    plat.add_foreign("llama-server", 3, path="/opt/homebrew/bin/llama-server")
    run_ticks(daemon, clock, 2)
    c = by(doctor.daemon_checks(daemon, home="/nonexistent"), 12)[0]
    assert "protective mode is ON" in c["message"] and "llama-server" in c["message"]


def test_doctor_local_checks_and_exit_code(short_tmp, monkeypatch, capsys):
    from gsched import cli as gcli
    monkeypatch.setenv("GSCHED_HOME", str(short_tmp / "h"))
    monkeypatch.delenv("GSCHED_SOCKET", raising=False)
    rc = gcli.main(["doctor", "--json"])
    out = json.loads(capsys.readouterr().out)
    checks = out["checks"]
    assert rc == 2                                       # the daemon is not running: check 2 FAILs
    assert [c for c in checks if c["id"] == 2][0]["level"] == "FAIL"
    assert [c for c in checks if c["id"] == 1][0]["level"] == "PASS"
    assert [c for c in checks if c["id"] == 14][0]["level"] == "PASS"


def test_doctor_names_the_binary_that_needs_full_disk_access(daemon):
    import os, sys
    c = by(doctor.daemon_checks(daemon), 18)[0]
    real = os.path.realpath(sys.executable)
    assert c["level"] == "INFO" and (real in c["message"] or sys.executable in c["message"])
    if real != os.path.abspath(sys.executable):          # a symlinked venv: the doctor prints the realpath to grant
        assert f"grant Full Disk Access to {real}" in c["message"]

import ast
import json
import os
import signal
import socket
from pathlib import Path

import pytest

from gsched.clock import FakeClock, SystemClock
from gsched.config import Config
from gsched.errors import ConfigError
from gsched.platform import JobIdentity
from gsched.platform.fake import FakePlatform
from gsched.units import GiB, bytes_to_gib, gib_to_bytes

ROOT = Path(__file__).resolve().parents[2]


def test_units_gib_exact():
    assert gib_to_bytes(1) == 2**30
    assert gib_to_bytes(20) == 20 * 2**30
    assert gib_to_bytes(0.5) == 2**29
    assert gib_to_bytes(1.1) == int(round(1.1 * 2**30))
    assert bytes_to_gib(3 * GiB) == 3.0


DOC_DEFAULTS = {
    "enforce": False, "timezone": None, "night_window": {"start": "23:00", "end": "08:00"},
    "day_max_s": 7200, "backfill": False, "morning_unload_restartable": True, "allow_spill": False,
    "on_timeout": "wait", "grace_s": 120, "kill_grace_s": 15, "coop_min_hold_s": 300,
    "restartable_min_hold_s": 60, "preempt_restartable": False, "preempt_grace_ms": 180000,
    "fair_halflife_s": 21600, "starve_s": 14400, "os_reserve_gib": None, "headroom_gib": 2,
    "gpu_shared_max": 2, "cpu_budget": None, "footprint_note_gib": 2,
    "heavy_basenames": ["llama-server", "vllm", "ollama"], "heavy_listen_ports": [], "port_probe_s": 5,
    "protective_unload_restartable": True, "protective_clear_s": 30,
    "pressure": {
        "warn": {"level_below": 30, "swap_rise_gib_per_min": 1.0, "reclaimable_below_gib": 4},
        "critical": {"level_below": 15, "swap_rise_gib_per_min": 4.0, "reclaimable_below_gib": 1.5}},
    "pressure_checkpoint_cooperative": False, "pressure_step_s": 10, "checkpoint_signal": "SIGUSR1",
    "attach_grace_ms": 30000, "spawn_report_timeout_ms": 10000, "prevent_sleep": True,
    "base_path": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "socket_path": None,
    "log_max_bytes": 5000000, "retention_days": 30, "mcp_max_wait_s": 30, "gpu_busy_foreign_pct": None,
    "launchd_label": "com.gpuscheduler.agent", "default_project": None,
    # PR 12 (docs/implementation/16)
    "reservations": [], "default_unknown_estimate_s": 7200, "job_log_max_bytes": 1073741824,
    "job_log_keep_bytes": 67108864, "tcc_protected_roots": [], "yield_apps": [],
}


def test_config_defaults_match_doc():
    assert Config.defaults().as_dict() == DOC_DEFAULTS


def test_config_missing_file_is_defaults(tmp_path):
    assert Config.load(tmp_path / "nope.json").enforce is False


@pytest.mark.parametrize("raw,key", [
    ({"kill_grace_s": 0}, "kill_grace_s"),
    ({"grace_s": 100000}, "grace_s"),
    ({"enforce": "yes"}, "enforce"),
    ({"night_window": {"start": "23:00", "end": "23:00"}}, "night_window"),
    ({"night_window": {"start": "25:00", "end": "08:00"}}, "night_window"),
    ({"checkpoint_signal": "SIGTERM"}, "checkpoint_signal"),
    ({"heavy_listen_ports": [0]}, "heavy_listen_ports"),
    ({"timezone": "Not/AZone"}, "timezone"),
    ({"gpu_shared_max": 0}, "gpu_shared_max"),
    ({"pressure": {"warn": {"bogus": 1}}}, "pressure.warn"),
    ({"headroom_gib": float("inf")}, "headroom_gib"),
])
def test_config_invalid_value_names_key(raw, key):
    with pytest.raises(ConfigError) as e:
        Config.from_dict(raw)
    assert e.value.key == key


def test_config_unknown_key_logged_once(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"_comment": "x", "bogus": 1, "enforce": True}))
    seen, got = set(), []
    c = Config.load(p, got.append, seen)
    assert c.enforce is True
    Config.load(p, got.append, seen)
    assert got == ["bogus"]


def test_config_nan_rejected(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"headroom_gib": NaN}')
    with pytest.raises(ConfigError):
        Config.load(p)


def test_config_replace_validates():
    assert Config.defaults().replace(enforce=True).enforce
    with pytest.raises(ConfigError):
        Config.defaults().replace(grace_s=0)


def test_fake_clock_never_sleeps(monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: pytest.fail("slept"))
    c = FakeClock(1000)
    c.advance(5)
    assert c.now_ms() == 6000
    m0 = c.mono()
    c.advance(2.5)
    assert c.mono() - m0 == 2.5
    c.set_wall(0)
    assert c.now_ms() == 0 and c.mono() - m0 == 2.5
    assert SystemClock().now_ms() > 0


def _scan_signal_calls(path: Path):
    tree = ast.parse(path.read_text())
    bad = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            f = n.func
            full = f"{f.value.id}.{f.attr}" if isinstance(f.value, ast.Name) else f".{f.attr}"
            if full in ("os.kill", "os.killpg", "signal.pthread_kill", "signal.raise_signal") or \
                    f.attr in ("send_signal", "terminate", "kill") and full not in ("os.kill",):
                bad.append((n.lineno, full))
    return bad


def test_only_platform_sends_signals():
    allowed = {"darwin.py", "fake.py"}
    offenders = []
    for p in (ROOT / "gsched").rglob("*.py"):
        if p.name in allowed and p.parent.name == "platform":
            continue
        for line, what in _scan_signal_calls(p):
            offenders.append(f"{p.relative_to(ROOT)}:{line} {what}")
    assert not offenders, offenders


def test_darwin_signals_only_in_signal_functions():
    """In darwin.py, os.kill / os.killpg appear only in signal_pid, signal_group and pid_alive (signal 0)."""
    src = (ROOT / "gsched/platform/darwin.py").read_text()
    tree = ast.parse(src)
    where = {}
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for n in ast.walk(fn):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("kill", "killpg") \
                        and isinstance(n.func.value, ast.Name) and n.func.value.id == "os":
                    where.setdefault(fn.name, []).append(n.func.attr)
    assert set(where) == {"signal_pid", "signal_group", "pid_alive"}, where


def test_fake_signal_log(plat, tmp_path):
    ident = _spawn(plat, tmp_path)
    assert plat.signal_pid(ident, signal.SIGUSR1)
    assert plat.signals[-1][:2] == (ident.pid, int(signal.SIGUSR1))
    assert plat.signal_group(ident, signal.SIGTERM)
    assert plat.signals[-1][0] == -ident.pgid


def _spawn(plat, tmp_path, job="j1"):
    r, w = os.pipe()
    gr, gw = os.pipe()
    d = tmp_path / job
    d.mkdir(exist_ok=True)
    shim = plat.spawn_shim(["/usr/bin/python3", "-I", "-m", "gsched.shim", "--job", job, "--gate-fd", str(gr),
                            "--report-fd", str(w), "--exit-file", str(d / "exit.json"), "--", "sleep", "1"],
                           env={}, cwd=str(d), stdout_path=str(d / "o"), stderr_path=str(d / "e"), pass_fds=(gr, w))
    os.close(gr)
    os.close(w)
    wl_pid = int(os.read(r, 64).strip())
    p = plat.proc(wl_pid)
    ident = JobIdentity(wl_pid, p.start_sec, p.start_usec, p.path, p.pgid)
    assert p.ppid == shim and p.pgid == shim
    plat._test_gate = gw
    return ident


def test_fake_gate_release_and_exit(plat, tmp_path):
    ident = _spawn(plat, tmp_path)
    gw = plat._test_gate
    os.write(gw, b"1")
    p = plat.proc(ident.pid)
    assert p.path == "/opt/fake/bin/sleep"
    plat.workload("j1").exit(3)
    assert plat.proc(ident.pid) is None
    rec = json.loads((tmp_path / "j1" / "exit.json").read_text())
    assert rec["exit_code"] == 3
    assert len(plat.reap()) == 1


def test_fake_gate_close_exits_111(plat, tmp_path):
    ident = _spawn(plat, tmp_path)
    os.close(plat._test_gate)
    assert plat.proc(ident.pid) is None
    assert json.loads((tmp_path / "j1" / "exit.json").read_text())["exit_code"] == 111


def test_fake_identity_recycle(plat, tmp_path):
    ident = _spawn(plat, tmp_path)
    assert plat.identity_matches(ident)
    plat.recycle_pid(ident.pid)
    assert not plat.identity_matches(ident)
    assert not plat.signal_pid(ident, signal.SIGTERM)
    assert plat.signals == []


def test_fake_foreign_and_memory(plat):
    base = plat.mem_sample(0).reclaimable_bytes
    pid = plat.add_foreign("llama-server", 16, port=8089)
    assert plat.port_listening(8089)
    assert base - plat.mem_sample(0).reclaimable_bytes == 16 * GiB
    plat.set_footprint_unreadable(pid)
    assert [p for p in plat.processes() if p.pid == pid][0].footprint_bytes is None
    plat.set_mem(swap_unreadable=True)
    assert plat.mem_sample(0).swap_used_bytes is None


def test_fake_cooperative_signal(plat, tmp_path):
    ident = _spawn(plat, tmp_path)
    os.write(plat._test_gate, b"1")
    w = plat.workload("j1")
    ck = tmp_path / "ck"
    w.env["GSCHED_CHECKPOINT_DIR"] = str(ck)
    w.on_signal(signal.SIGUSR1, "checkpoint_and_exit")
    p = plat.proc(ident.pid)  # path changed at exec
    assert not plat.signal_pid(ident, signal.SIGUSR1)
    ident = JobIdentity(p.pid, p.start_sec, p.start_usec, p.path, p.pgid)
    assert plat.signal_pid(ident, signal.SIGUSR1)
    assert (ck / "READY").read_text().startswith("ready\n")
    assert json.loads((tmp_path / "j1" / "exit.json").read_text())["exit_code"] == 0

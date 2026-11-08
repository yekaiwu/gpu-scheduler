import os

import pytest

from gsched.config import Config
from gsched.scan import Scanner
from gsched.units import GiB

CFG = Config.defaults()


def scan(plat, rows=(), cfg=CFG, mono=0.0):
    sc = getattr(plat, "_scanner", None)
    if sc is None:
        sc = plat._scanner = Scanner(plat)
    return sc.classify(plat.processes(), list(rows), cfg, mono)


def test_browser_footprint_is_record_only(plat):
    plat.add_foreign("Browser", 6, path="/Applications/Browser.app/Contents/MacOS/Browser")
    r = scan(plat)
    assert [o.cls for o in r.noted] == ["noted"] and not r.foreign_heavy and plat.signals == []


def test_small_footprint_not_noted(plat):
    plat.add_foreign("tiny", 0.5)
    assert not scan(plat).noted


def test_foreign_heavy_any_footprint(plat):
    pid = plat.add_foreign("llama-server", 0.01, path="/opt/homebrew/bin/llama-server")
    r = scan(plat)
    assert [o.pid for o in r.foreign_heavy] == [pid]
    assert not r.noted


def test_other_user_heavy_basename_counts(plat):
    pid = plat.add_foreign("ollama", 0.0, path="/usr/local/bin/ollama", uid=os.getuid() + 1)
    assert [o.pid for o in scan(plat).foreign_heavy] == [pid]


def test_foreign_port_listener_is_heavy(plat):
    plat.add_foreign("mystery", 0.2, port=8089)
    r = scan(plat, cfg=CFG.replace(heavy_listen_ports=(8089,)))
    ports = [o for o in r.foreign_heavy if o.port == 8089]
    assert len(ports) == 1 and ports[0].pid is not None and r.ports_listening == {8089: True}


def test_our_listen_port_is_not_foreign(plat):
    plat.add_foreign("server", 0.2, port=8089)
    cfg = CFG.replace(heavy_listen_ports=(8089,))
    assert [o for o in scan(plat, cfg=cfg).foreign_heavy if o.port == 8089]
    sc = Scanner(plat)
    r = sc.classify(plat.processes(), [_row(pid=1, listen_port=8089)], cfg, 0.0)
    assert not [o for o in r.foreign_heavy if o.port == 8089]


def _row(**kw):
    base = {"id": "j1", "pid": None, "pgid": None, "shim_pid": None, "spawn": "daemon", "listen_port": None}
    base.update(kw)
    return base


def test_descendant_llama_server_is_ours_not_foreign(plat):
    from tests.scan.helpers import start_job
    row, w = start_job(plat, "j1")
    cpid = w.spawn_child("llama-server", 12, path="/opt/homebrew/bin/llama-server")
    r = scan(plat, [row])
    assert cpid in r.trees["j1"] and not r.foreign_heavy


def test_job_footprint_sums_tree_including_setsid_child(plat):
    from tests.scan.helpers import start_job
    row, w = start_job(plat, "j1")
    w.set_footprint(3)
    w.spawn_child("a", 2)
    w.spawn_child("b", 4, setsid=True)          # left the group, found through the ppid chain
    r = scan(plat, [row])
    assert r.job_footprint["j1"] == 9 * GiB      # the shim itself is not counted


def test_unreadable_workload_footprint_is_none(plat):
    from tests.scan.helpers import start_job
    row, w = start_job(plat, "j1")
    plat.set_footprint_unreadable(w.pid)
    w.spawn_child("a", 2)
    assert scan(plat, [row]).job_footprint["j1"] is None


def test_orphan_shim_detected(plat):
    plat.add_foreign("python3", 0.05, path="/x/venv/bin/python3",
                     argv=["/x/venv/bin/python3", "-I", "-m", "gsched.shim", "--job", "zzz"])
    r = scan(plat)
    assert len(r.orphan_shims) == 1


def test_known_shim_is_not_orphan(plat):
    from tests.scan.helpers import start_job
    row, w = start_job(plat, "j1")
    r = scan(plat, [row])
    assert not r.orphan_shims and not r.foreign_heavy


def test_job_memory_counts_mmapped_resident_pages(plat):
    """llama.cpp maps GGUF weights: phys_footprint ~8 GiB, resident ~26 GiB (measured live 2026-11-08).
    The job's memory must be max(footprint, resident) so pending = reservation - used is not double-counted."""
    from tests.scan.helpers import start_job
    row, w = start_job(plat, "j1")
    w.set_footprint(8)
    w.set_resident(26)
    r = scan(plat, [row])
    assert r.job_footprint["j1"] == 26 * GiB


def test_resident_not_used_for_foreign_noted_classification(plat):
    pid = plat.add_foreign("Browser", 1, path="/Applications/Browser.app/Contents/MacOS/Browser")
    plat._procs[pid].resident = 6 * GiB          # big RSS (shared maps) alone must not make it "noted"
    assert not scan(plat).noted


def test_shim_of_another_home_is_not_our_orphan(plat, tmp_path):
    """Regression (live 2026-11-08): a pytest run's shims (another GSCHED_HOME) put the real daemon in protective mode."""
    mine = tmp_path / "home" / "jobs"
    other = tmp_path / "other" / "jobs" / "x" / "exit.json"
    plat.add_foreign("python3", 0.05, path="/x/venv/bin/python3",
                     argv=["/x/venv/bin/python3", "-I", "-m", "gsched.shim", "--job", "x", "--exit-file", str(other)])
    plat.add_foreign("python3", 0.05, path="/x/venv/bin/python3",
                     argv=["/x/venv/bin/python3", "-I", "-m", "gsched.shim", "--job", "y",
                           "--exit-file", str(mine / "y" / "exit.json")])
    sc = Scanner(plat, jobs_dir=str(mine))
    r = sc.classify(plat.processes(), [], CFG, 0.0)
    assert len(r.orphan_shims) == 1                     # only the shim pointing into OUR jobs dir

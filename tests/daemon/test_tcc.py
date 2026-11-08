"""TCC guard (urgent addendum to PR 12): under launchd, touching ~/Documents without consent BLOCKS the caller. The
single-threaded daemon must never open, stat, list or chdir into a protected root; a hook below makes any such call
fail the test, simulating the hang."""

import os
import sys

import pytest

from gsched.config import Config
from gsched.platform.fake import SIGTERM
from tests.daemon.conftest import ClientError, client, project, run_ticks, spec


@pytest.fixture
def uhome(short_tmp):
    h = short_tmp / "uhome"
    h.mkdir()
    return str(h)


@pytest.fixture
def tdaemon(make_daemon, plat, uhome):
    """A daemon whose TCC probes are answered by the fake platform. Returns (daemon, home)."""
    for r in ("Documents", "Desktop", "Downloads", "Library/Mobile Documents"):
        plat.tcc[f"{uhome}/{r}"] = "ok"
    d = make_daemon(tcc_home=uhome)
    project(d, "p")
    return d


@pytest.fixture
def hang_hook(monkeypatch, uhome):
    """Any in-process filesystem call on a path under the user's protected folders would hang a real daemon."""
    touched = []
    guarded = ("listdir", "stat", "lstat", "access", "open", "scandir", "chdir", "readlink")
    for name in guarded:
        real = getattr(os, name)

        def make(real, name):
            def wrapper(path, *a, **k):
                try:
                    p = os.fspath(path) if not isinstance(path, int) else ""
                except TypeError:
                    p = ""
                if isinstance(p, bytes):
                    p = os.fsdecode(p)
                if p.startswith(uhome + "/Documents") or p.startswith(uhome + "/Desktop") \
                        or p.startswith(uhome + "/Downloads") or p.startswith(uhome + "/Library/Mobile"):
                    touched.append((name, p))
                    raise AssertionError(f"os.{name}({p!r}) would block the daemon behind a TCC prompt")
                return real(path, *a, **k)
            return wrapper
        monkeypatch.setattr(os, name, make(real, name))
    import builtins
    real_open = builtins.open

    def guarded_open(file, *a, **k):
        if isinstance(file, (str, bytes, os.PathLike)):
            p = os.fsdecode(os.fspath(file))
            if p.startswith((uhome + "/Documents", uhome + "/Desktop", uhome + "/Downloads")):
                touched.append(("open", p))
                raise AssertionError(f"open({p!r}) would block the daemon behind a TCC prompt")
        return real_open(file, *a, **k)
    monkeypatch.setattr(builtins, "open", guarded_open)
    return touched


def submit(d, cwd, **over):
    return client(d, "submit", spec=spec(cwd=cwd, **over), raw=True)


def test_probe_runs_at_startup_for_every_root_and_never_in_process(tdaemon, plat, uhome):
    roots = tdaemon.tcc.roots()
    assert {os.path.relpath(r, uhome) for r in roots} == {"Documents", "Desktop", "Downloads", "Library/Mobile Documents"}
    probes = [a for a in plat.async_spawned if "os.listdir" in " ".join(a)]
    assert len(probes) == 4
    for a in probes:
        assert a[:3] == [sys.executable, "-I", "-c"] and a[3] == "import os,sys; os.listdir(sys.argv[1]); print('ok')"
    assert all(tdaemon.tcc.state(r) == "ok" for r in roots)


def test_states_ok_denied_blocked_absent(make_daemon, plat, uhome, clock):
    plat.tcc[f"{uhome}/Documents"] = "ok"
    plat.tcc[f"{uhome}/Desktop"] = "denied"
    plat.tcc[f"{uhome}/Downloads"] = "blocked"
    plat.tcc[f"{uhome}/Library/Mobile Documents"] = "absent"
    d = make_daemon(tcc_home=uhome)
    st = lambda n: d.tcc.state(f"{uhome}/{n}")
    assert (st("Documents"), st("Desktop"), st("Downloads"), st("Library/Mobile Documents")) == \
        ("ok", "denied", "unknown", "absent")                    # the blocked probe has not answered yet
    run_ticks(d, clock, 2)
    assert st("Downloads") == "unknown" and not plat.async_killed
    run_ticks(d, clock, 2)                                       # 3 s: SIGKILL the (stuck) probe, state blocked
    assert st("Downloads") == "blocked"
    assert plat.async_killed and plat.async_killed[0][-1] == f"{uhome}/Downloads"
    e = d.tcc.entries[f"{uhome}/Downloads"]
    assert "3 s" in e.detail
    assert [c for c in d.tcc.report() if c["root"].endswith("Downloads")][0]["state"] == "blocked"


def test_reprobe_every_600s(tdaemon, plat, clock):
    n0 = len(plat.async_spawned)
    run_ticks(tdaemon, clock, 599)
    assert len(plat.async_spawned) == n0
    run_ticks(tdaemon, clock, 2)
    assert len(plat.async_spawned) == n0 + 4                      # all four roots probed again


def test_cwd_under_ok_root_is_accepted_without_touching_it(tdaemon, hang_hook, uhome):
    cwd = f"{uhome}/Documents/project/sub"
    r = submit(tdaemon, cwd)
    assert r["ok"], r
    assert hang_hook == []                                        # no stat/listdir/open/chdir under the root
    assert tdaemon.store.get(r["result"]["job"])["cwd"] == os.path.realpath(uhome) + "/Documents/project/sub"


def test_cwd_under_blocked_root_refused_fast_with_prompt_hint(tdaemon, plat, uhome, clock, hang_hook):
    plat.tcc[f"{uhome}/Documents"] = "blocked"
    tdaemon.tcc.entries.pop(f"{uhome}/Documents")
    tdaemon.tcc.start_all()
    run_ticks(tdaemon, clock, 4)                                  # the probe times out -> blocked
    assert tdaemon.tcc.state(f"{uhome}/Documents") == "blocked"
    r = submit(tdaemon, f"{uhome}/Documents/x")
    e = r["error"]
    assert e["code"] == "cwd_not_accessible" and e["detail"]["reason"] == "tcc_blocked"
    assert "Full Disk Access" in e["detail"]["hint"] and os.path.realpath(sys.executable) in e["detail"]["hint"]
    assert "prompt" in e["detail"]["hint"]
    assert hang_hook == []
    assert tdaemon.store.live_rows() == []


def test_cwd_under_denied_root_refused(tdaemon, plat, uhome, clock):
    plat.tcc[f"{uhome}/Desktop"] = "denied"
    tdaemon.tcc.entries.pop(f"{uhome}/Desktop")
    tdaemon.tcc.start_all()
    e = submit(tdaemon, f"{uhome}/Desktop/x")["error"]
    assert e["code"] == "cwd_not_accessible" and e["detail"]["reason"] == "tcc_denied"


def test_unknown_state_starts_probe_replies_retry_and_does_not_block(tdaemon, plat, uhome, clock, hang_hook):
    plat.tcc[f"{uhome}/Documents"] = "slow:2"
    tdaemon.tcc.entries.pop(f"{uhome}/Documents")
    tdaemon.tcc.start_all()                                      # in flight, no answer yet: state unknown
    n = len([a for a in plat.async_spawned if a[-1] == f"{uhome}/Documents"])
    e = submit(tdaemon, f"{uhome}/Documents/x")["error"]
    assert e["code"] == "cwd_not_accessible"
    assert e["detail"]["reason"] == "tcc_unknown_probe_started" and e["detail"]["tcc_unknown_probe_started"] is True
    assert "retry in a few seconds" in e["detail"]["hint"]
    assert len([a for a in plat.async_spawned if a[-1] == f"{uhome}/Documents"]) == n     # no second probe stacked
    run_ticks(tdaemon, clock, 3)
    assert tdaemon.tcc.state(f"{uhome}/Documents") == "ok"
    assert submit(tdaemon, f"{uhome}/Documents/x")["ok"] and hang_hook == []


def test_unknown_with_no_probe_running_starts_one_on_demand(tdaemon, plat, uhome):
    root = f"{uhome}/Downloads"
    tdaemon.tcc.entries.pop(root)
    plat.tcc[root] = "blocked"
    before = len(plat.async_spawned)
    e = submit(tdaemon, root + "/x")["error"]
    assert e["detail"]["reason"] == "tcc_unknown_probe_started"
    assert len(plat.async_spawned) == before + 1 and root in tdaemon.tcc.pending


def test_symlink_into_protected_root_is_not_followed_into_it(tdaemon, short_tmp, uhome, plat, hang_hook, clock):
    link = short_tmp / "elsewhere"
    link.symlink_to(f"{uhome}/Documents/proj")                   # a link OUTSIDE the root pointing inside
    plat.tcc[f"{uhome}/Documents"] = "denied"
    tdaemon.tcc.entries.pop(f"{uhome}/Documents")
    tdaemon.tcc.start_all()
    e = submit(tdaemon, str(link))["error"]
    assert e["code"] == "cwd_not_accessible" and e["detail"]["reason"] == "tcc_denied"
    assert hang_hook == []
    # .. components cannot be used to sneak in either
    e2 = submit(tdaemon, f"{uhome}/Documents/../Documents/y")["error"]
    assert e2["detail"]["reason"] == "tcc_denied"


def test_extra_roots_from_config(make_daemon, plat, short_tmp, uhome):
    extra = str(short_tmp / "vault")
    plat.tcc[extra] = "denied"
    d = make_daemon(Config.defaults().replace(enforce=True, timezone="UTC", tcc_protected_roots=(extra,)),
                    tcc_home=uhome)
    project(d, "p")
    assert extra in d.tcc.roots() and d.tcc.state(extra) == "denied"
    assert submit(d, extra + "/job")["error"]["detail"]["reason"] == "tcc_denied"


def test_command_under_protected_root_is_not_stat_ed(tdaemon, uhome, hang_hook):
    r = client(tdaemon, "submit", raw=True,
               spec=spec(cwd="/tmp", command=[f"{uhome}/Documents/venv/bin/python", "x.py"]))
    assert r["ok"] and hang_hook == []                           # assumed present; the workload's own error shows in logs


def test_path_entries_in_protected_root_are_skipped_not_searched(tdaemon, uhome, hang_hook):
    env = {"PATH": f"{uhome}/Documents/venv/bin:/usr/bin:/bin"}
    ok = client(tdaemon, "submit", raw=True, spec=spec(cwd="/tmp", command=["definitely-not-a-real-cmd"], env=env))
    assert ok["ok"] and hang_hook == []                          # could live in the protected entry: assumed to resolve
    found = client(tdaemon, "submit", raw=True, spec=spec(cwd="/tmp", command=["sh", "-c", "true"], env=env))
    assert found["ok"] and hang_hook == []
    plain = client(tdaemon, "submit", raw=True, spec=spec(cwd="/tmp", command=["definitely-not-a-real-cmd"]))
    assert plain["error"]["code"] == "command_not_found"         # no protected entry: the usual answer


def test_log_paths_in_protected_root_are_invalid(tdaemon, uhome, hang_hook):
    for fld in ("stdout", "stderr"):
        r = client(tdaemon, "submit", raw=True, spec=spec(cwd="/tmp", **{fld: f"{uhome}/Documents/out.log"}))
        assert r["error"]["code"] == "invalid" and r["error"]["detail"]["field"] == fld
        assert "TCC-protected root" in r["error"]["message"]
    assert hang_hook == []
    ok = client(tdaemon, "submit", raw=True, spec=spec(cwd="/tmp", stdout=f"{uhome}/elsewhere.log"))
    assert ok["ok"]


def test_external_job_cwd_in_protected_root_is_recorded_not_checked(make_daemon, plat, uhome, hang_hook):
    plat.tcc[f"{uhome}/Documents"] = "blocked"
    d = make_daemon(tcc_home=uhome)
    project(d, "p")
    s = spec(cwd=f"{uhome}/Documents/pipeline", spawn="external")
    del s["command"]
    r = client(d, "submit", spec=s, raw=True)
    assert r["ok"] and hang_hook == []


def test_profile_ensure_cwd_uses_the_guard(tdaemon, uhome):
    import json
    with open(os.path.join(tdaemon.paths.profiles, "srv.json"), "w") as fh:
        json.dump({"name": "srv", "project": "p",
                   "spec": {"command": ["sh", "-c", "sleep 1"], "cwd": f"{uhome}/Documents/models",
                            "resources": {"memory_gib": 1, "gpu": "none"}}}, fh)
    tdaemon.reload_all()
    tdaemon.tcc.entries[f"{uhome}/Documents"].state = "denied"
    with pytest.raises(ClientError) as e:
        client(tdaemon, "ensure", profile="srv")
    assert e.value.code == "cwd_not_accessible"


# ---- spawning: the daemon never chdir()s, the workload does -----------------------------------------------------------
def test_spawn_hands_cwd_to_the_shim_not_to_popen(tdaemon, clock, plat, short_tmp):
    jid = client(tdaemon, "submit", spec=spec(cwd="/tmp"))["job"]
    run_ticks(tdaemon, clock, 2)
    sh = plat._shims[jid]
    assert sh.cwd == tdaemon.paths.job_dir(jid)                   # Popen(cwd=...) is the job dir, never the user's cwd
    assert sh.target_cwd == os.path.realpath("/tmp")
    assert plat.workload(jid).released and tdaemon.supervisor.cwd_wait == {}      # cwd_ok arrived


def test_blocked_chdir_is_killed_and_fails_with_cwd_blocked(tdaemon, clock, plat):
    plat.blocked_cwds.add(os.path.realpath("/tmp"))
    jid = client(tdaemon, "submit", spec=spec(cwd="/tmp", checkpoint="atomic"))["job"]
    run_ticks(tdaemon, clock, 3)
    r = tdaemon.store.get(jid)
    assert r["held_reason"] == "live" and jid in tdaemon.supervisor.cwd_wait      # waiting, daemon not blocked
    client(tdaemon, "ping")
    run_ticks(tdaemon, clock, 10)                                 # spawn_report_timeout_ms = 10 s after the gate
    ev = tdaemon.store.events(kinds=["cwd_blocked"], since_ms=0, job=jid)
    assert ev and ev[0]["body"]["cwd"] == os.path.realpath("/tmp")
    assert [s for s in plat.signals if s[1] == SIGTERM]           # stuck in chdir(): nothing of the command ran
    run_ticks(tdaemon, clock, 3)
    v = client(tdaemon, "get", job=jid)
    assert v["state"] == "failed" and v["exit"]["end_reason"] == "cwd_blocked"
    assert tdaemon.supervisor.cwd_wait == {}
    # the slot is free again
    j2 = client(tdaemon, "submit", spec=spec(cwd="/var"))["job"]
    run_ticks(tdaemon, clock, 2)
    assert tdaemon.store.get(j2)["state"] == "held"


def test_cwd_wait_closed_when_job_ends_first(tdaemon, clock, plat):
    plat.blocked_cwds.add(os.path.realpath("/tmp"))
    jid = client(tdaemon, "submit", spec=spec(cwd="/tmp"))["job"]
    run_ticks(tdaemon, clock, 3)
    plat.workload(jid).exit(126)                                  # chdir() failed -> the shim exits 126
    run_ticks(tdaemon, clock, 3)
    assert tdaemon.supervisor.cwd_wait == {} and tdaemon.store.get(jid)["state"] == "failed"
    assert tdaemon.store.get(jid)["end_reason"] == "exit"


def test_doctor_uses_the_cache_and_never_lists(tdaemon, uhome, hang_hook, plat, clock):
    from gsched import doctor
    plat.tcc[f"{uhome}/Documents"] = "blocked"
    tdaemon.tcc.entries.pop(f"{uhome}/Documents")
    tdaemon.tcc.start_all()
    run_ticks(tdaemon, clock, 4)
    checks = [c for c in doctor.daemon_checks(tdaemon) if c["id"] == 7]
    docs = [c for c in checks if "Documents" in c["name"] and "Mobile" not in c["name"]][0]
    assert docs["level"] == "WARN" and "consent prompt" in docs["message"]
    assert hang_hook == []

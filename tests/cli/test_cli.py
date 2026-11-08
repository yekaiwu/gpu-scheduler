import json
import os
import subprocess
import sys
import threading

import pytest

from gsched import cli as gcli

JOB = {"job": "20261108T231500-7f3a", "state": "queued", "queue": "day", "warnings": []}


@pytest.mark.parametrize("code,exit_", [("unknown_project", 2), ("unknown_job", 2), ("forbidden", 2), ("too_big", 2),
                                         ("cwd_not_accessible", 2), ("command_not_found", 2), ("draining", 2),
                                         ("bad_state", 2), ("bad_lease", 2), ("still_running", 2),
                                         ("schema", 4), ("invalid", 4), ("internal", 4)])
def test_cli_exit_codes(fake, cli, code, exit_):
    fake.reply("get", error={"code": code, "message": "boom"})
    rc, out, err = cli("get", "x")
    assert rc == exit_ and json.loads(out)["error"]["code"] == code and "boom" in err


def test_cli_exit_0_and_json_when_not_tty(fake, cli):
    fake.reply("get", {"job": "j", "state": "held"})
    rc, out, _ = cli("get", "j")
    assert rc == 0 and out.strip() == '{"job":"j","state":"held"}'          # compact: stdout is not a TTY


def test_cli_submit_fills_cwd(fake, cli, tmp_path, monkeypatch):
    fake.reply("submit", JOB)
    spec = {"schema": "gpu-scheduler.job.v1", "project": "p", "command": ["true"], "checkpoint": "atomic",
            "resources": {"memory_gib": 1}}
    f = tmp_path / "s.json"
    f.write_text(json.dumps(spec))
    monkeypatch.chdir(tmp_path)
    rc, out, _ = cli("submit", "--file", str(f), "--after", "A", "--after", "B", "--priority", "5", "--queue", "night")
    assert rc == 0
    sent = fake.requests[-1]["params"]["spec"]
    assert sent["cwd"] == os.getcwd() and sent["after"] == ["A", "B"] and sent["priority"] == 5 and sent["queue"] == "night"
    rc, out, _ = cli("submit", "--file", str(f), "--print-id")
    assert out.strip() == JOB["job"]


def test_cli_submit_stdin_and_observe_hint(fake, cli, monkeypatch):
    import io
    fake.reply("submit", {"state": "observe", "job": None, "would": {"would": "admit_now"}, "hint": "observe mode!"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"project": "p", "resources": {"memory_gib": 1},
                                                              "checkpoint": "atomic", "command": ["x"]})))
    rc, out, err = cli("submit", "-f", "-")
    assert rc == 0 and "observe mode!" in err
    assert fake.requests[-1]["params"]["spec"]["schema"] == "gpu-scheduler.job.v1"


def test_cli_run_builds_spec(fake, cli):
    fake.reply("submit", JOB)
    rc, out, _ = cli("run", "--project", "latent", "--name", "n", "--mem", "20", "--gpu", "none", "--cpu", "4",
                     "--lock", "a", "--lock", "b", "--est", "1h30m", "--limit", "4h", "--checkpoint", "cooperative",
                     "--priority", "3", "--queue", "night", "--after", "X", "--allow-spill", "--env", "A=b",
                     "--cwd", "/tmp", "--", "python", "-c", "print(1)")
    assert rc == 0
    s = fake.requests[-1]["params"]["spec"]
    assert s["command"] == ["python", "-c", "print(1)"] and s["resources"] == {
        "memory_gib": 20.0, "gpu": "none", "cpu_cores": 4, "locks": ["a", "b"]}
    assert s["runtime"] == {"estimate_s": 5400, "limit_s": 14400} and s["checkpoint"] == "cooperative"
    assert s["env"] == {"A": "b"} and s["allow_spill"] is True and s["after"] == ["X"] and s["cwd"] == "/tmp"
    assert s["name"] == "n" and s["priority"] == 3 and s["queue"] == "night"


def test_cli_run_requires_mem_and_checkpoint(fake, cli, monkeypatch):
    monkeypatch.delenv("GSCHED_PROJECT", raising=False)
    fake.reply("suggest", {"n": 5, "memory_gib_p95": 14.5})
    rc, _, err = cli("run", "--project", "p", "--name", "n", "--checkpoint", "atomic", "--", "x")
    assert rc == 64 and "--mem is required" in err and "14.5" in err          # prints the history suggestion
    rc, _, err = cli("run", "--project", "p", "--mem", "4", "--", "x")
    assert rc == 64 and "--checkpoint" in err
    rc, _, err = cli("run", "--mem", "4", "--checkpoint", "atomic", "--", "x")
    assert rc == 64 and "no project" in err
    assert not [r for r in fake.requests if r["method"] == "submit"]


def test_cli_run_project_from_env(fake, cli, monkeypatch):
    monkeypatch.setenv("GSCHED_PROJECT", "envproj")
    fake.reply("submit", JOB)
    rc, _, _ = cli("run", "--mem", "1", "--checkpoint", "atomic", "--", "true")
    assert rc == 0 and fake.requests[-1]["params"]["spec"]["project"] == "envproj"


def test_cli_rejects_gb_unit(fake, cli):
    rc, _, err = cli("run", "--project", "p", "--mem", "20GB", "--checkpoint", "atomic", "--", "x")
    assert rc == 64 and "GiB" in err
    assert gcli.parse_mem("20") == 20 and gcli.parse_mem("20G") == 20 and gcli.parse_mem("20GiB") == 20
    assert gcli.parse_mem("512M") == 0.5 and gcli.parse_mem("512MiB") == 0.5 and gcli.parse_mem("1.5G") == 1.5
    with pytest.raises(gcli.UsageError):
        gcli.parse_mem("5 GB")


def test_cli_duration_parsing():
    p = gcli.parse_duration
    assert (p("90"), p("90s"), p("15m"), p("4h"), p("1d"), p("1h30m"), p("2d3h")) == (90, 90, 900, 14400, 86400, 5400, 183600)
    for bad in ("", "abc", "1.5h", "-3", "h1"):
        with pytest.raises(gcli.UsageError):
            p(bad)


def test_cli_connect_failure_exit_3(short_tmp, cli, monkeypatch):
    monkeypatch.setenv("GSCHED_SOCKET", str(short_tmp / "nope.sock"))
    monkeypatch.setenv("GSCHED_HOME", str(short_tmp / "home"))
    rc, out, err = cli("status")
    assert rc == 3 and "not running" in err and "gsched daemon" in err


def test_cli_wait_retries_then_3(short_tmp, cli, monkeypatch):
    from gsched import client
    monkeypatch.setenv("GSCHED_SOCKET", str(short_tmp / "nope.sock"))
    sleeps = []
    t = [0.0]
    monkeypatch.setattr(client.time, "sleep", lambda s: (sleeps.append(s), t.__setitem__(0, t[0] + s)))
    monkeypatch.setattr(client.time, "monotonic", lambda: t[0])
    rc, _, err = cli("wait", "x")
    assert rc == 3 and len(sleeps) >= 50          # retried for ~30 s (0.5 s steps) before giving up


def test_cli_wait_exit_codes(fake, cli):
    fake.reply("wait", {"job": "j", "state": "done"})
    assert cli("wait", "j")[0] == 0
    fake.reply("wait", {"job": "j", "state": "failed"})
    assert cli("wait", "j")[0] == 2
    fake.reply("wait", {"job": "j", "state": "cancelled"})
    assert cli("wait", "j")[0] == 2
    fake.reply("wait", {"job": "j", "held_reason": "await_attach", "lease": "L" * 64})
    rc, out, _ = cli("wait", "j", "--until", "start")
    assert rc == 0 and json.loads(out)["lease"] == "L" * 64


def test_cli_wait_timeout_124(fake, cli):
    fake.reply("wait", {"job": "j", "state": "done"})
    fake.delay["wait"] = threading.Event()               # the daemon never answers
    rc, _, err = cli("wait", "j", "--timeout", "1")
    fake.delay["wait"].set()
    assert rc == 124 and "timed out" in err


def test_cli_get_wait_param(fake, cli):
    fake.reply("get", {"job": "j", "state": "held"})
    cli("get", "j", "--wait", "30s")
    assert fake.requests[-1]["params"] == {"job": "j", "wait_s": 30}


def test_cli_list_defaults_non_terminal(fake, cli):
    fake.reply("list", {"jobs": []})
    cli("list")
    assert fake.requests[-1]["params"]["state"] == ["needs_estimate", "queued", "held"]
    cli("list", "--all", "--project", "p", "--limit", "5")
    assert "state" not in fake.requests[-1]["params"] and fake.requests[-1]["params"]["project"] == "p"
    cli("list", "--state", "done", "--state", "failed")
    assert fake.requests[-1]["params"]["state"] == ["done", "failed"]


def test_cli_list_table_format(capsys):
    gcli.print_table([{"job": "20261108T231700-01bc", "project": "example-train", "name": "tokenize",
                       "state": "queued", "blocked_by": ["memory_ledger"], "resources": {"memory_gib": 12, "gpu": "none"},
                       "priority": 0, "queue": "day"}])
    out = capsys.readouterr().out
    assert "memory_ledger" in out and "12.0G" in out and out.splitlines()[0].startswith("ID")


def test_cli_cancel_and_estimate(fake, cli):
    fake.reply("cancel", {"job": "j", "state": "cancelled", "action": "cancelled"})
    cli("cancel", "j", "--terminate")
    assert fake.requests[-1]["params"] == {"terminate": True, "job": "j"}
    cli("cancel", "--lease", "L")
    assert fake.requests[-1]["params"] == {"terminate": False, "lease": "L"}
    rc, _, err = cli("cancel")
    assert rc == 64
    fake.reply("estimate", {"job": "j"})
    cli("estimate", "j", "3h")
    assert fake.requests[-1]["params"] == {"job": "j", "estimate_s": 10800}


def test_cli_logs(fake, cli):
    fake.reply("logs", {"data": "hello\n", "offset": 6, "eof": True, "size": 6})
    rc, out, _ = cli("logs", "j", "--stderr", "--tail", "100")
    assert out == "hello\n" and fake.requests[-1]["params"] == {"job": "j", "stream": "stderr", "tail_bytes": 100}


def test_cli_logs_follow_until_terminal(fake, cli):
    chunks = iter([{"data": "a\n", "offset": 2, "eof": False, "size": 4}, {"data": "b\n", "offset": 4, "eof": True, "size": 4},
                   {"data": "", "offset": 4, "eof": True, "size": 4}])
    fake.script["logs"] = lambda req: {"rpc": "gpu-scheduler.rpc.v1", "ok": True, "result": next(chunks, {"data": "", "offset": 4, "eof": True, "size": 4})}
    fake.reply("get", {"job": "j", "state": "done"})
    rc, out, _ = cli("logs", "j", "--follow")
    assert rc == 0 and out == "a\nb\n"
    offs = [r["params"].get("offset") for r in fake.requests if r["method"] == "logs"]
    assert offs[0] is None and offs[1] == 2


def test_cli_status_brief(fake, cli):
    fake.reply("status", {"machine": {"memsize_gib": 48}, "memory": {"budget_gib": 40, "reserved_gib": 32, "live_free_gib": 9.7},
                          "gpu": {"exclusive_job": "x", "shared_jobs": []}, "pressure": "normal", "running": [1],
                          "queued": 3, "enforce": True})
    rc, out, _ = cli("status", "--brief")
    assert out.strip() == ("48.0 GiB machine · budget 40.0 · reserved 32.0 · live free 9.7 · GPU exclusive(1) · pressure normal"
                           " · 1 running · 3 queued · enforce on")


def test_cli_profile_ops_and_launcher_cmds(fake, cli):
    for m in ("ensure", "inhibit", "uninhibit", "hold", "unhold"):
        fake.reply(m, {"profile": "llama"})
        assert cli(m, "llama")[0] == 0 and fake.requests[-1]["method"] == m
    fake.reply("drain", {"draining": True})
    cli("drain")
    assert fake.requests[-1]["method"] == "drain"
    fake.reply("undrain", {"draining": False})
    cli("undrain")
    assert fake.requests[-1]["method"] == "undrain"
    fake.reply("attach", {"job": "j"})
    cli("attach", "--lease", "L", "--pid", "123")
    assert fake.requests[-1]["params"] == {"lease": "L", "pid": 123}
    fake.reply("finish", {"job": "j"})
    cli("finish", "--lease", "L", "--exit-code", "3")
    assert fake.requests[-1]["params"] == {"lease": "L", "exit_code": 3}
    fake.reply("release", {"job": "j"})
    cli("release", "--lease", "L")
    fake.reply("suggest", {"n": 0})
    cli("suggest", "--project", "p", "--name", "n")
    assert fake.requests[-1]["params"] == {"project": "p", "name": "n"}


def test_cli_run_wait(fake, cli):
    fake.reply("submit", JOB)
    fake.reply("wait", {"job": JOB["job"], "state": "done", "exit": {"code": 0}})
    rc, out, _ = cli("run", "--project", "p", "--mem", "1", "--checkpoint", "atomic", "--wait", "--", "true")
    assert rc == 0 and fake.requests[-1]["method"] == "wait"
    fake.reply("wait", {"job": JOB["job"], "state": "failed"})
    rc, _, _ = cli("run", "--project", "p", "--mem", "1", "--checkpoint", "atomic", "--wait", "--", "true")
    assert rc == 2


def test_cli_help_does_not_import_daemon_or_mcp():
    code = ("import sys; from gsched import cli; "
            "rc = cli.main(['--help']) if False else None; "
            "p = cli.build_parser(); "
            "bad = [m for m in sys.modules if m in ('gsched.daemon','gsched.mcp_stdio','gsched.store','gsched.tick','gsched.supervise')]; "
            "print(bad)")
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, cwd="/tmp")
    assert out.stdout.strip() == "[]", out.stderr
    out = subprocess.run([sys.executable, "-m", "gsched", "--help"], capture_output=True, text=True)
    assert out.returncode == 0 and "usage: gsched" in out.stdout


def test_cli_usage_error_exit_64(cli):
    rc, _, _ = cli("bogus-command")
    assert rc == 64
    rc, _, _ = cli()
    assert rc == 64


def test_config_set_validates_before_write(short_tmp, cli, monkeypatch):
    home = short_tmp / "h"
    monkeypatch.setenv("GSCHED_HOME", str(home))
    monkeypatch.setenv("GSCHED_SOCKET", str(short_tmp / "none.sock"))
    rc, out, _ = cli("config", "set", "enforce", "true")
    assert rc == 0 and json.load(open(home / "config.json")) == {"enforce": True}
    assert oct(os.stat(home / "config.json").st_mode & 0o777) == "0o600"
    before = open(home / "config.json").read()
    rc, _, err = cli("config", "set", "grace_s", "0")
    assert rc == 4 and "grace_s" in err and open(home / "config.json").read() == before
    rc, _, err = cli("config", "set", "not_a_key", "1")
    assert rc == 64
    rc, out, _ = cli("config", "set", "default_project", "myproj")
    assert json.load(open(home / "config.json"))["default_project"] == "myproj"
    rc, out, _ = cli("config", "get", "default_project")
    assert json.loads(out) == "myproj"
    rc, out, _ = cli("config", "get")
    assert json.loads(out)["enforce"] is True and json.loads(out)["grace_s"] == 120


def test_project_add_mode_0600(short_tmp, cli, monkeypatch):
    home = short_tmp / "h"
    monkeypatch.setenv("GSCHED_HOME", str(home))
    monkeypatch.setenv("GSCHED_SOCKET", str(short_tmp / "none.sock"))
    rc, out, _ = cli("project", "add", "demo", "--weight", "3", "--mcp-submit", "--default-cwd", "/tmp")
    assert rc == 0
    p = home / "projects" / "demo.json"
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    assert json.load(open(p)) == {"name": "demo", "weight": 3, "mcp_submit": True, "mcp_terminate": False,
                                  "default_cwd": "/tmp"}
    rc, _, err = cli("project", "add", "demo")
    assert rc == 2 and "--force" in err
    assert cli("project", "add", "demo", "--force")[0] == 0
    rc, out, _ = cli("project", "list")
    assert json.loads(out)["projects"][0]["name"] == "demo"
    assert cli("project", "add", "Bad Name")[0] == 64


def test_default_project_from_config(fake, cli, short_tmp, monkeypatch):
    home = short_tmp / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"default_project": "dflt"}))
    monkeypatch.delenv("GSCHED_PROJECT", raising=False)
    fake.reply("submit", JOB)
    rc, _, _ = cli("run", "--mem", "1", "--checkpoint", "atomic", "--", "true")
    assert rc == 0 and fake.requests[-1]["params"]["spec"]["project"] == "dflt"


def test_cli_run_print_id(fake, cli):
    fake.reply("submit", {"job": "20261108T000000-abcd", "state": "queued", "queue": "day", "warnings": []})
    rc, out, _ = cli("run", "--print-id", "--project", "p", "--mem", "1", "--gpu", "none", "--checkpoint", "atomic",
                     "--", "true")
    assert rc == 0 and out.strip() == "20261108T000000-abcd"

import copy
import json
from types import SimpleNamespace

import pytest

from gsched.config import Config
from gsched.errors import GschedError
from gsched.spec import assign_queue, job_env, parse_spec, spec_json_schema, SCHEMA_ID

CFG = Config.defaults()


def base(**over):
    d = {"schema": SCHEMA_ID, "project": "p", "command": ["true"], "checkpoint": "atomic",
         "resources": {"memory_gib": 1}, "runtime": {"estimate_s": 60}}
    d.update(over)
    return d


def parse(d, door="cli", cfg=CFG):
    return parse_spec(d, cfg, door=door)


def code(d, door="cli"):
    with pytest.raises(GschedError) as e:
        parse(d, door)
    return e.value


def test_schema_mismatch_is_error_4():
    e = code(base(schema="other"))
    assert e.code == "schema" and e.exit_code == 4
    assert code({"project": "p"}).code == "schema"
    assert code([1]).code == "schema"


def test_unknown_key_rejected():
    e = code(base(memory_gb=3))
    assert e.code == "invalid" and e.detail["unknown_keys"] == ["memory_gb"]
    d = base()
    d["resources"]["bogus"] = 1
    assert code(d).code == "invalid"
    d = base()
    d["runtime"] = {"estimate": 1}
    assert code(d).code == "invalid"


def test_client_cannot_set_state_columns():
    for k in ("resume", "attempt", "hold", "ready", "switch_to", "state"):
        assert code(base(**{k: 1})).code == "invalid"


def test_bool_is_not_int():
    d = base(priority=True)
    assert code(d).detail["field"] == "priority"
    d = base()
    d["resources"]["cpu_cores"] = True
    assert code(d).detail["field"] == "resources.cpu_cores"
    assert code(base(allow_spill=1)).detail["field"] == "allow_spill"
    d = base()
    d["resources"]["memory_gib"] = True
    assert code(d).code == "invalid"


def test_nan_rejected():
    d = base()
    d["resources"]["memory_gib"] = float("nan")
    assert code(d).code == "invalid"
    d["resources"]["memory_gib"] = float("inf")
    assert code(d).code == "invalid"


def test_checkpoint_required():
    d = base()
    del d["checkpoint"]
    assert code(d).detail["field"] == "checkpoint"
    assert code(base(checkpoint="whatever")).code == "invalid"


def test_memory_required_and_bounds():
    d = base()
    del d["resources"]["memory_gib"]
    assert code(d).detail["field"] == "resources.memory_gib"
    for v in (0, -1, 5000):
        d = base()
        d["resources"]["memory_gib"] = v
        assert code(d).code == "invalid"


def test_kill_rejected_for_atomic():
    assert code(base(on_timeout="kill")).detail["field"] == "on_timeout"
    assert parse(base(checkpoint="cooperative", on_timeout="kill")).on_timeout == "kill"


def test_gsched_env_reserved():
    assert code(base(env={"GSCHED_X": "1"})).code == "invalid"
    assert code(base(env={"1bad": "1"})).code == "invalid"
    assert code(base(env={"A": 1})).code == "invalid"
    assert parse(base(env={"A": "1"})).env == {"A": "1"}


def test_port_lock_reserved_for_profiles():
    d = base()
    d["resources"]["locks"] = ["port:8080"]
    assert code(d).code == "invalid"
    assert parse(d, "profile").locks == ("port:8080",)
    d["resources"]["locks"] = ["a", "a"]
    assert code(d).code == "invalid"


def test_mcp_rejects_external_spawn():
    d = base(spawn="external")
    del d["command"]
    e = code(d, "mcp")
    assert e.code == "forbidden"
    assert parse(d, "cli").spawn == "external"


def test_command_required_for_daemon():
    d = base()
    del d["command"]
    assert code(d).detail["field"] == "command"
    assert code(base(command=[])).code == "invalid"
    assert code(base(command=[""])).code == "invalid"
    assert code(base(command=["a\0"])).code == "invalid"


def test_cwd_must_be_absolute():
    assert code(base(cwd="rel/dir")).detail["field"] == "cwd"
    assert parse(base(cwd="/a/../b")).cwd == "/b"


def test_defaults():
    s = parse(base())
    assert s.gpu == "exclusive" and s.cpu_cores == 1 and s.priority == 0 and s.queue_requested == "auto"
    assert s.grace_s == 120 and s.on_timeout == "wait" and s.allow_spill is False and s.spawn == "daemon"
    s2 = parse(base(), cfg=CFG.replace(grace_s=33, allow_spill=True))
    assert s2.grace_s == 33 and s2.allow_spill is True


def asq(cp="atomic", est=None, q="auto", cfg=CFG):
    return assign_queue(SimpleNamespace(checkpoint=cp, estimate_s=est, queue_requested=q), cfg)


def test_auto_queue_boundary_7200_is_day():
    assert asq(est=7200) == ("day", "queued")
    assert asq(est=7201) == ("night", "queued")


def test_atomic_without_estimate_needs_estimate():
    assert asq("atomic") == (None, "needs_estimate")
    assert asq("cooperative", q="night") == (None, "needs_estimate")


def test_restartable_may_omit_estimate():
    assert asq("restartable") == ("day", "queued")
    assert asq("restartable", q="night") == ("night", "queued")


def test_night_window_null_everything_day():
    cfg = CFG.replace(night_window=None)
    assert asq("atomic", est=999999, cfg=cfg) == ("day", "queued")
    assert asq("atomic", None, "night", cfg=cfg) == ("day", "queued")


def test_job_env_is_minimal_plus_spec(monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN", "x")
    s = parse(base(env={"PATH": "/custom", "A": "b"}))
    user = SimpleNamespace(home="/home/u", name="u", shell="/bin/zsh")
    env = job_env(s, "jid", 2, True, "/ck", CFG, user)
    assert "SECRET_TOKEN" not in env
    assert env["PATH"] == "/custom" and env["A"] == "b" and env["HOME"] == "/home/u"
    assert env["GSCHED_JOB_ID"] == "jid" and env["GSCHED_ATTEMPT"] == "2" and env["GSCHED_RESUME"] == "1"
    assert env["GSCHED_CHECKPOINT_DIR"] == "/ck" and env["PYTHONUNBUFFERED"] == "1"
    assert env["GSCHED_MEMORY_BYTES"] == str(s.memory_bytes)
    env2 = job_env(s, "jid", 1, False, "/ck", CFG, user)
    assert env2["GSCHED_RESUME"] == "0"


def test_comment_truncated_on_codepoint():
    s = parse(base(comment="é" * 150))
    assert len(s.comment.encode()) <= 200 and s.comment == "é" * 100
    assert parse(base(comment="a" * 300)).comment == "a" * 200


def test_memory_gib_to_bytes_exact():
    d = base()
    d["resources"]["memory_gib"] = 20
    assert parse(d).memory_bytes == 20 * 2**30
    d["resources"]["memory_gib"] = 0.5
    assert parse(d).memory_bytes == 2**29


def test_limits_and_ranges():
    d = base()
    d["runtime"]["estimate_s"] = 31 * 86400
    assert code(d).code == "invalid"
    assert code(base(priority=101)).code == "invalid"
    assert code(base(project="Bad Name")).code == "invalid"
    d = base()
    d["resources"]["level_min"] = 101
    assert code(d).code == "invalid"


def test_schema_generated_from_fields():
    sch = spec_json_schema()
    assert set(sch["required"]) >= {"schema", "project", "checkpoint", "resources"}
    assert sch["properties"]["resources"]["required"] == ["memory_gib"]
    assert "schema" not in spec_json_schema(schema_required=False)["required"]
    json.dumps(sch)

"""Parse, validate and normalize `gpu-scheduler.job.v1` (03). Pure: no filesystem, no clock.

A single FIELDS table drives both the validator and `spec_json_schema()` (used by the MCP adapter).
"""

from __future__ import annotations

import math
import os
import pwd
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from gsched.config import Config
from gsched.errors import GschedError
from gsched.units import gib_to_bytes

SCHEMA_ID = "gpu-scheduler.job.v1"
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
LOCK_RE = re.compile(r"^[A-Za-z0-9:._/-]{1,128}$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
JOB_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{4}$")
MAX_ESTIMATE_S = 30 * 86400


@dataclass(frozen=True)
class FieldDef:
    path: str
    type: str                   # str | int | num | bool | list[str] | map[str,str]
    required: bool = False
    nullable: bool = False
    default: Any = None         # None = no static default (null, or filled from cfg)
    default_note: str = ""
    enum: tuple | None = None
    min: float | None = None
    max: float | None = None
    exclusive_min: bool = False
    max_len: int | None = None
    desc: str = ""


FIELDS: tuple[FieldDef, ...] = (
    FieldDef("schema", "str", required=True, enum=(SCHEMA_ID,), desc="Always gpu-scheduler.job.v1."),
    FieldDef("project", "str", required=True, max_len=64, desc="Project name (a project file must exist)."),
    FieldDef("name", "str", nullable=True, max_len=128, desc="Free label for history grouping and display."),
    FieldDef("command", "list[str]", nullable=True, desc="argv. Required unless spawn=external."),
    FieldDef("cwd", "str", nullable=True, desc="Absolute working directory (the daemon must be able to read it)."),
    FieldDef("env", "map[str,str]", default={}, desc="Extra environment variables. GSCHED_* is reserved."),
    FieldDef("resources.memory_gib", "num", required=True, min=0, max=4096, exclusive_min=True,
             desc="Memory reservation in GiB (peak you expect)."),
    FieldDef("resources.gpu", "str", default="exclusive", enum=("exclusive", "shared", "none"),
             desc="GPU use. none = CPU-only."),
    FieldDef("resources.cpu_cores", "int", default=1, min=1, max=1024, desc="CPU cores to reserve."),
    FieldDef("resources.locks", "list[str]", default=[], desc="Named locks (one holder at a time)."),
    FieldDef("resources.ceiling_gib", "num", nullable=True, min=0, exclusive_min=True,
             desc="Footprint above this is reported (restartable jobs are unloaded)."),
    FieldDef("resources.level_min", "int", nullable=True, min=0, max=100,
             desc="Minimum kern.memorystatus_level required to start."),
    FieldDef("runtime.estimate_s", "int", nullable=True, min=0, max=MAX_ESTIMATE_S, exclusive_min=True,
             desc="Expected runtime in seconds. Required for atomic/cooperative when the night window is on."),
    FieldDef("runtime.limit_s", "int", nullable=True, min=0, exclusive_min=True,
             desc="Runtime limit in seconds, counted from process start."),
    FieldDef("checkpoint", "str", required=True, enum=("restartable", "cooperative", "atomic"),
             desc="What the program can do. atomic if unsure."),
    FieldDef("priority", "int", default=0, min=-100, max=100, desc="Higher runs first."),
    FieldDef("queue", "str", default="auto", enum=("auto", "day", "night"), desc="auto: night if estimate_s > day_max_s."),
    FieldDef("after", "list[str]", default=[], desc="Job ids that must reach done first."),
    FieldDef("spawn", "str", default="daemon", enum=("daemon", "external"), desc="Who starts the process."),
    FieldDef("grace_s", "int", nullable=True, min=1, max=86400, default_note="config grace_s",
             desc="Seconds a cooperative job gets to checkpoint."),
    FieldDef("on_timeout", "str", nullable=True, enum=("wait", "kill"), default_note="config on_timeout",
             desc="After grace_s: wait (default) or kill. kill is rejected for atomic."),
    FieldDef("allow_spill", "bool", nullable=True, default_note="config allow_spill",
             desc="Let an atomic night job run past the window end."),
    FieldDef("idempotency_key", "str", nullable=True, max_len=128, desc="Dedupe key per project."),
    FieldDef("comment", "str", default="", desc="Log-only note (truncated to 200 bytes)."),
    FieldDef("stdout", "str", nullable=True, desc="Absolute path for stdout (default: the job dir)."),
    FieldDef("stderr", "str", nullable=True, desc="Absolute path for stderr (default: the job dir)."),
)
_BY_PATH = {f.path: f for f in FIELDS}
_TOP_KEYS = {f.path for f in FIELDS if "." not in f.path} | {"resources", "runtime"}
_SUB_KEYS = {"resources": {f.path.split(".")[1] for f in FIELDS if f.path.startswith("resources.")},
             "runtime": {f.path.split(".")[1] for f in FIELDS if f.path.startswith("runtime.")}}


@dataclass(frozen=True, slots=True)
class JobSpec:
    project: str
    name: str | None
    command: tuple[str, ...] | None
    cwd: str | None
    env: Mapping[str, str]
    memory_bytes: int
    gpu: Literal["exclusive", "shared", "none"]
    cpu_cores: int
    locks: tuple[str, ...]
    ceiling_bytes: int | None
    level_min: int | None
    estimate_s: int | None
    limit_s: int | None
    checkpoint: Literal["restartable", "cooperative", "atomic"]
    priority: int
    queue_requested: Literal["auto", "day", "night"]
    after: tuple[str, ...]
    spawn: Literal["daemon", "external"]
    grace_s: int
    on_timeout: Literal["wait", "kill"]
    allow_spill: bool
    idempotency_key: str | None
    comment: str
    stdout_path: str | None
    stderr_path: str | None


def _invalid(field: str, msg: str, **kw) -> GschedError:
    return GschedError("invalid", f"{field}: {msg}", field=field, **kw)


def _check_scalar(f: FieldDef, v: Any) -> Any:
    """Type and range check for one field. Returns the value (ints stay ints)."""
    if v is None:
        if f.nullable:
            return None
        raise _invalid(f.path, "must not be null")
    t = f.type
    if t == "str":
        if not isinstance(v, str):
            raise _invalid(f.path, "must be a string")
        if f.max_len is not None and len(v) > f.max_len:
            raise _invalid(f.path, f"must be at most {f.max_len} characters")
        if f.enum is not None and v not in f.enum:
            raise _invalid(f.path, f"must be one of {', '.join(f.enum)}")
        return v
    if t == "bool":
        if not isinstance(v, bool):
            raise _invalid(f.path, "must be a JSON boolean")
        return v
    if t in ("int", "num"):
        if isinstance(v, bool) or not isinstance(v, (int,) if t == "int" else (int, float)):
            raise _invalid(f.path, "must be a JSON integer" if t == "int" else "must be a number")
        if isinstance(v, float) and not math.isfinite(v):
            raise _invalid(f.path, "must be finite")
        if f.min is not None and (v <= f.min if f.exclusive_min else v < f.min):
            raise _invalid(f.path, f"must be {'>' if f.exclusive_min else '>='} {f.min:g}")
        if f.max is not None and v > f.max:
            raise _invalid(f.path, f"must be <= {f.max:g}")
        return v
    return v


def _str_list(path: str, v: Any, *, max_items: int, max_item_len: int | None = None) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise _invalid(path, "must be a list of strings")
    if len(v) > max_items:
        raise _invalid(path, f"at most {max_items} items")
    if max_item_len is not None and any(len(x.encode()) > max_item_len for x in v):
        raise _invalid(path, f"items must be at most {max_item_len} bytes")
    return v


def _abs_path(path: str, v: str) -> str:
    if "\0" in v:
        raise _invalid(path, "must not contain NUL")
    p = os.path.expanduser(v)
    if not os.path.isabs(p):
        raise _invalid(path, "must be an absolute path")
    return os.path.normpath(p)


def parse_spec(obj: object, cfg: Config, *, door: Literal["cli", "mcp", "profile"]) -> JobSpec:
    if not isinstance(obj, dict):
        raise GschedError("schema", "job spec must be a JSON object")
    if obj.get("schema") != SCHEMA_ID:
        raise GschedError("schema", f"schema must be exactly {SCHEMA_ID!r}", got=obj.get("schema"))
    unknown = sorted(k for k in obj if k not in _TOP_KEYS)
    if unknown:
        raise GschedError("invalid", f"unknown keys: {', '.join(unknown)}", field=unknown[0], unknown_keys=unknown)
    sub: dict[str, dict] = {}
    for grp in ("resources", "runtime"):
        g = obj.get(grp, {})
        if g is None:
            g = {}
        if not isinstance(g, dict):
            raise _invalid(grp, "must be an object")
        bad = sorted(k for k in g if k not in _SUB_KEYS[grp])
        if bad:
            raise GschedError("invalid", f"unknown keys in {grp}: {', '.join(bad)}", field=f"{grp}.{bad[0]}",
                              unknown_keys=[f"{grp}.{b}" for b in bad])
        sub[grp] = g

    def get(path: str) -> Any:
        f = _BY_PATH[path]
        src = obj if "." not in path else sub[path.split(".")[0]]
        key = path.split(".")[-1]
        if key not in src:
            if f.required:
                raise _invalid(path, "is required")
            return f.default if not isinstance(f.default, (list, dict)) else type(f.default)(f.default)
        return _check_scalar(f, src[key])

    get("schema")
    project = get("project")
    if not PROJECT_RE.match(project):
        raise _invalid("project", "must match ^[a-z0-9][a-z0-9._-]{0,63}$")
    name = get("name")
    if name is not None and not name.isprintable():
        raise _invalid("name", "must be printable")
    spawn = get("spawn")
    # command
    command = obj.get("command")
    if command is None:
        if spawn == "daemon":
            raise _invalid("command", "is required for spawn=daemon")
        cmd_t = None
    else:
        lst = _str_list("command", command, max_items=4096, max_item_len=32 * 1024)
        if not lst or not lst[0]:
            raise _invalid("command", "command[0] must be non-empty")
        if any("\0" in x for x in lst):
            raise _invalid("command", "must not contain NUL")
        if sum(len(x.encode()) for x in lst) > 256 * 1024:
            raise _invalid("command", "total size must be at most 256 KiB")
        cmd_t = tuple(lst)
    cwd = get("cwd")
    if cwd is not None:
        cwd = _abs_path("cwd", cwd)
    # env
    env = obj.get("env", {})
    if env is None:
        env = {}
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise _invalid("env", "must be an object of string values")
    if len(env) > 256:
        raise _invalid("env", "at most 256 keys")
    for k, v in env.items():
        if not ENV_KEY_RE.match(k):
            raise _invalid("env", f"invalid variable name {k!r}")
        if k.startswith("GSCHED_"):
            raise _invalid("env", f"{k}: the GSCHED_ prefix is reserved")
        if len(v.encode()) > 32 * 1024 or "\0" in v:
            raise _invalid("env", f"value of {k} is too large or contains NUL")
    # resources
    mem = get("resources.memory_gib")
    gpu = get("resources.gpu")
    cpu = get("resources.cpu_cores")
    locks_raw = sub["resources"].get("locks", [])
    locks = _str_list("resources.locks", locks_raw if locks_raw is not None else [], max_items=16)
    if len(set(locks)) != len(locks):
        raise _invalid("resources.locks", "must be unique")
    for l in locks:
        if not LOCK_RE.match(l):
            raise _invalid("resources.locks", f"invalid lock name {l!r}")
        if l.startswith("port:") and door != "profile":
            raise _invalid("resources.locks", "the port: prefix is reserved for profiles")
    ceiling = get("resources.ceiling_gib")
    level_min = get("resources.level_min")
    est = get("runtime.estimate_s")
    limit = get("runtime.limit_s")
    checkpoint = get("checkpoint")
    priority = get("priority")
    queue = get("queue")
    after_raw = obj.get("after", [])
    after = _str_list("after", after_raw if after_raw is not None else [], max_items=64)
    if any(not x or len(x) > 128 for x in after):
        raise _invalid("after", "job ids must be non-empty strings")
    if door == "mcp" and spawn == "external":
        raise GschedError("forbidden", "use the gsched CLI for external launchers", field="spawn")
    grace = get("grace_s")
    on_timeout = get("on_timeout")
    allow_spill = get("allow_spill")
    if grace is None:
        grace = cfg.grace_s
    if on_timeout is None:
        on_timeout = cfg.on_timeout
    if allow_spill is None:
        allow_spill = cfg.allow_spill
    if on_timeout == "kill" and checkpoint == "atomic":
        raise _invalid("on_timeout", "kill is not allowed for atomic jobs")
    idem = get("idempotency_key")
    comment = get("comment")
    if not isinstance(comment, str):
        raise _invalid("comment", "must be a string")
    comment = comment.encode("utf-8")[:200].decode("utf-8", "ignore")
    so, se = get("stdout"), get("stderr")
    so = _abs_path("stdout", so) if so is not None else None
    se = _abs_path("stderr", se) if se is not None else None
    return JobSpec(
        project=project, name=name, command=cmd_t, cwd=cwd, env=dict(env),
        memory_bytes=gib_to_bytes(mem), gpu=gpu, cpu_cores=cpu, locks=tuple(locks),
        ceiling_bytes=gib_to_bytes(ceiling) if ceiling is not None else None, level_min=level_min,
        estimate_s=est, limit_s=limit, checkpoint=checkpoint, priority=priority, queue_requested=queue,
        after=tuple(after), spawn=spawn, grace_s=grace, on_timeout=on_timeout, allow_spill=allow_spill,
        idempotency_key=idem, comment=comment, stdout_path=so, stderr_path=se)


def assign_queue(spec, cfg: Config) -> tuple[str | None, str]:
    """(queue, initial_state). `spec` needs checkpoint, estimate_s, queue_requested."""
    if cfg.night_window is None:
        return ("day", "queued")
    if spec.checkpoint in ("atomic", "cooperative") and spec.estimate_s is None:
        return (None, "needs_estimate")
    if spec.queue_requested in ("day", "night"):
        return (spec.queue_requested, "queued")
    if spec.estimate_s is None:
        return ("day", "queued")
    return ("night" if spec.estimate_s > cfg.day_max_s else "day", "queued")


def job_env(spec, job_id: str, attempt: int, resume: bool, checkpoint_dir: str, cfg: Config, user) -> dict[str, str]:
    env = {
        "PATH": cfg.base_path,
        "HOME": user.home, "USER": user.name, "LOGNAME": user.name,
        "SHELL": user.shell, "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
    }
    env.update(spec.env)
    env.update({
        "GSCHED_JOB_ID": job_id,
        "GSCHED_ATTEMPT": str(attempt),
        "GSCHED_RESUME": "1" if resume else "0",
        "GSCHED_CHECKPOINT_DIR": checkpoint_dir,
        "GSCHED_PROJECT": spec.project,
        "GSCHED_MEMORY_BYTES": str(spec.memory_bytes),
        "PYTHONUNBUFFERED": env.get("PYTHONUNBUFFERED", "1"),
    })
    return env


@dataclass(frozen=True)
class UserInfo:
    name: str
    home: str
    shell: str


def current_user() -> UserInfo:
    pw = pwd.getpwuid(os.getuid())
    return UserInfo(pw.pw_name, pw.pw_dir, pw.pw_shell)


# ---- JSON Schema generation (MCP) ------------------------------------------------------------
def _field_schema(f: FieldDef) -> dict:
    t = {"str": {"type": "string"}, "int": {"type": "integer"}, "num": {"type": "number"},
         "bool": {"type": "boolean"}, "list[str]": {"type": "array", "items": {"type": "string"}},
         "map[str,str]": {"type": "object", "additionalProperties": {"type": "string"}}}[f.type]
    s = dict(t)
    if f.nullable:
        s["type"] = [s["type"], "null"]
    if f.enum:
        s["enum"] = list(f.enum)
    if f.type in ("int", "num"):
        if f.min is not None:
            s["exclusiveMinimum" if f.exclusive_min else "minimum"] = f.min
        if f.max is not None:
            s["maximum"] = f.max
    if f.max_len is not None:
        s["maxLength"] = f.max_len
    if f.desc:
        s["description"] = f.desc
    if f.default is not None:
        s["default"] = f.default
    elif f.default_note:
        s["description"] = (s.get("description", "") + f" Default: {f.default_note}.").strip()
    return s


def spec_json_schema(*, schema_required: bool = True) -> dict:
    props: dict[str, Any] = {}
    required: list[str] = []
    groups = {"resources": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
              "runtime": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}
    for f in FIELDS:
        if "." in f.path:
            g, k = f.path.split(".")
            groups[g]["properties"][k] = _field_schema(f)
            if f.required:
                groups[g]["required"].append(k)
        else:
            props[f.path] = _field_schema(f)
            if f.required and (f.path != "schema" or schema_required):
                required.append(f.path)
    for g, v in groups.items():
        if not v["required"]:
            del v["required"]
        props[g] = v
    required.append("resources")
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}

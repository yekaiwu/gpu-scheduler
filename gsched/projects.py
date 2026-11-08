"""Project files (13) and resident profiles (08 section 2)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Callable

from gsched.errors import GschedError

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
PROJECT_KEYS = {"name", "weight", "mcp_submit", "mcp_terminate", "default_cwd", "command_allow"}


@dataclass(frozen=True)
class Project:
    name: str
    weight: int = 1
    mcp_submit: bool = False
    mcp_terminate: bool = False
    default_cwd: str | None = None
    command_allow: tuple | None = None


def parse_project(stem: str, raw: object, on_unknown: Callable[[str], None] | None = None) -> Project:
    if not isinstance(raw, dict):
        raise ValueError("project file must be a JSON object")
    if raw.get("name") != stem:
        raise ValueError(f"name {raw.get('name')!r} must equal the filename stem {stem!r}")
    if not NAME_RE.match(stem):
        raise ValueError("invalid project name")
    w = raw.get("weight", 1)
    if isinstance(w, bool) or not isinstance(w, int) or w < 1:
        raise ValueError("weight must be an integer >= 1")
    for k in ("mcp_submit", "mcp_terminate"):
        if not isinstance(raw.get(k, False), bool):
            raise ValueError(f"{k} must be a boolean")
    dc = raw.get("default_cwd")
    if dc is not None and (not isinstance(dc, str) or not os.path.isabs(os.path.expanduser(dc))):
        raise ValueError("default_cwd must be an absolute path or null")
    ca = raw.get("command_allow")
    if ca is not None and (not isinstance(ca, list) or not all(isinstance(x, str) and x for x in ca)):
        raise ValueError("command_allow must be a list of path prefixes or null")
    for k in raw:
        if k not in PROJECT_KEYS and on_unknown:
            on_unknown(k)
    return Project(stem, w, raw.get("mcp_submit", False), raw.get("mcp_terminate", False),
                   os.path.expanduser(dc) if dc else None, tuple(ca) if ca is not None else None)


def _dir_signature(path: str) -> tuple:
    try:
        entries = sorted(os.scandir(path), key=lambda e: e.name)
        return (os.stat(path).st_mtime_ns,) + tuple((e.name, e.stat().st_mtime_ns) for e in entries
                                                    if e.name.endswith(".json"))
    except FileNotFoundError:
        return ()


class ProjectRegistry:
    """Loads `$H/projects/*.json`; re-reads when the directory signature changes."""

    def __init__(self, directory: str, warn: Callable[[str, dict], None] | None = None):
        self.dir = directory
        self.warn = warn or (lambda kind, body: None)
        self._sig: tuple | None = None
        self._projects: dict[str, Project] = {}
        self._unknown_seen: set = set()

    def reload_if_changed(self, force: bool = False) -> bool:
        sig = _dir_signature(self.dir)
        if not force and sig == self._sig:
            return False
        self._sig = sig
        projects: dict[str, Project] = {}
        try:
            names = sorted(f for f in os.listdir(self.dir) if f.endswith(".json"))
        except FileNotFoundError:
            names = []
        for fn in names:
            stem = fn[:-5]
            try:
                with open(os.path.join(self.dir, fn), "rb") as fh:
                    raw = json.loads(fh.read())
                def unk(k, stem=stem):
                    if (stem, k) not in self._unknown_seen:
                        self._unknown_seen.add((stem, k))
                        self.warn("config_unknown_key", {"file": fn, "key": k})
                projects[stem] = parse_project(stem, raw, unk)
            except (OSError, ValueError) as e:
                self.warn("config_reload_failed", {"file": fn, "error": str(e)})
        self._projects = projects
        return True

    def get(self, name: str) -> Project:
        p = self._projects.get(name)
        if p is None:
            raise GschedError("unknown_project", f"no project file for {name!r}; "
                              f"create one with `gsched project add {name}`", project=name)
        return p

    def find(self, name: str) -> Project | None:
        return self._projects.get(name)

    def all(self) -> list[Project]:
        return list(self._projects.values())

    def names(self) -> list[str]:
        return sorted(self._projects)


# ---- resident profiles (08 section 2) ---------------------------------------------------------------------------
import re as _re

from gsched.policy.model import AbortLines

URL_RE = _re.compile(r"^http://(127\.0\.0\.1|localhost|\[::1\]):(\d{1,5})(/[^\s]*)?$")
_VAR_RE = _re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
PROFILE_KEYS = {"name", "project", "spec", "listen_port", "health_url", "slots_url", "health_bearer_file",
                "idle_unload_s", "hold", "admit", "reason", "abort_level_below", "abort_reclaimable_below_gib",
                "abort_swap_rise_gib", "abort_ceiling_gib"}


@dataclass(frozen=True)
class Profile:
    name: str
    project: str
    spec: object                      # gsched.spec.JobSpec (restartable, daemon, day)
    listen_port: int | None
    health_url: str | None
    slots_url: str | None
    health_bearer_file: str | None
    idle_unload_s: int | None
    hold: bool
    admit: bool
    reason: str | None
    abort: AbortLines


def read_env_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def expand_vars(value, env: dict[str, str]):
    """${VAR} from the env file first, then the daemon environment. An unset variable is an error: no partial expansion."""
    if isinstance(value, str):
        def sub(m):
            k = m.group(1)
            if k in env:
                return env[k]
            if k in os.environ:
                return os.environ[k]
            raise ValueError(f"environment variable {k} is not set (define it in $GSCHED_HOME/env)")
        return _VAR_RE.sub(sub, value)
    if isinstance(value, list):
        return [expand_vars(v, env) for v in value]
    if isinstance(value, dict):
        return {k: expand_vars(v, env) for k, v in value.items()}
    return value


def load_profile(stem: str, raw: object, cfg, registry: "ProjectRegistry", env: dict[str, str]) -> Profile:
    from gsched.errors import GschedError
    from gsched.spec import SCHEMA_ID, parse_spec
    if not isinstance(raw, dict):
        raise ValueError("profile file must be a JSON object")
    if raw.get("name") != stem:
        raise ValueError(f"name {raw.get('name')!r} must equal the filename stem {stem!r}")
    unknown = sorted(set(raw) - PROFILE_KEYS)
    if unknown:
        raise ValueError(f"unknown keys: {', '.join(unknown)}")
    project = raw.get("project")
    if not isinstance(project, str) or registry.find(project) is None:
        raise ValueError(f"project {project!r} does not exist")
    spec = raw.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("spec must be an object")
    if spec.get("checkpoint", "restartable") != "restartable":
        raise ValueError("a profile is a resident: checkpoint must be restartable")
    spec = expand_vars(spec, env)
    port = raw.get("listen_port")
    if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535):
        raise ValueError("listen_port must be 1..65535 or null")
    urls = {}
    for k in ("health_url", "slots_url", "health_bearer_file"):
        v = expand_vars(raw.get(k), env)
        if v is not None and not isinstance(v, str):
            raise ValueError(f"{k} must be a string or null")
        if k != "health_bearer_file" and v is not None and not URL_RE.match(v):
            raise ValueError(f"{k} must be a loopback http URL (127.0.0.1, localhost or [::1])")
        urls[k] = v
    full = dict(spec)
    full.update({"schema": SCHEMA_ID, "project": project, "checkpoint": "restartable", "spawn": "daemon",
                 "queue": "day", "name": stem})
    res = dict(full.get("resources") or {})
    locks = list(res.get("locks") or [])
    if port is not None and f"port:{port}" not in locks:
        locks.append(f"port:{port}")
    res["locks"] = locks
    full["resources"] = res
    try:
        js = parse_spec(full, cfg, door="profile")
    except GschedError as e:
        raise ValueError(e.message)
    idle = raw.get("idle_unload_s")
    if idle is not None and (isinstance(idle, bool) or not isinstance(idle, int) or idle <= 0):
        raise ValueError("idle_unload_s must be a positive integer or null")
    for k in ("hold", "admit"):
        if not isinstance(raw.get(k, k == "admit"), bool):
            raise ValueError(f"{k} must be a boolean")
    nums = {}
    for k, kind in (("abort_level_below", int), ("abort_reclaimable_below_gib", float), ("abort_swap_rise_gib", float),
                    ("abort_ceiling_gib", float)):
        v = raw.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
            raise ValueError(f"{k} must be a non-negative number or null")
        nums[k] = v
    return Profile(
        name=stem, project=project, spec=js, listen_port=port, health_url=urls["health_url"],
        slots_url=urls["slots_url"], health_bearer_file=urls["health_bearer_file"], idle_unload_s=idle,
        hold=bool(raw.get("hold", False)), admit=bool(raw.get("admit", True)), reason=raw.get("reason"),
        abort=AbortLines(nums["abort_level_below"], nums["abort_reclaimable_below_gib"], nums["abort_swap_rise_gib"],
                         nums["abort_ceiling_gib"]))


class ProfileRegistry:
    def __init__(self, directory: str, env_file: str, projects: ProjectRegistry, get_cfg):
        self.dir, self.env_file, self.projects, self.get_cfg = directory, env_file, projects, get_cfg
        self._sig: tuple | None = None
        self.profiles: dict[str, Profile] = {}
        self.errors: dict[str, str] = {}

    def reload_if_changed(self, force: bool = False) -> list[tuple[str, str]]:
        """Returns newly failing (profile, error) pairs."""
        sig = (_dir_signature(self.dir), _dir_signature(os.path.dirname(self.env_file) or ".")[:1],
               self.projects._sig)
        try:
            sig = sig + (os.stat(self.env_file).st_mtime_ns,)
        except FileNotFoundError:
            pass
        if not force and sig == self._sig:
            return []
        self._sig = sig
        env = read_env_file(self.env_file)
        profiles, errors, new_errors = {}, {}, []
        try:
            names = sorted(f for f in os.listdir(self.dir) if f.endswith(".json"))
        except FileNotFoundError:
            names = []
        for fn in names:
            stem = fn[:-5]
            try:
                with open(os.path.join(self.dir, fn), "rb") as fh:
                    raw = json.loads(fh.read())
                profiles[stem] = load_profile(stem, raw, self.get_cfg(), self.projects, env)
            except (OSError, ValueError) as e:
                errors[stem] = str(e)
                if self.errors.get(stem) != str(e):
                    new_errors.append((stem, str(e)))
        self.profiles, self.errors = profiles, errors
        return new_errors

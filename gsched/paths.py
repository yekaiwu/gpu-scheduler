"""Filesystem layout under GSCHED_HOME (01 section 6)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from gsched.errors import PlatformError

DEFAULT_HOME = "~/.local/gpu-scheduler"
SUN_PATH_MAX = 104


def resolve_home(home: str | os.PathLike | None = None) -> str:
    h = os.fspath(home) if home is not None else os.environ.get("GSCHED_HOME") or DEFAULT_HOME
    return os.path.realpath(os.path.expanduser(h))


@dataclass(frozen=True)
class Paths:
    home: str

    @classmethod
    def resolve(cls, home=None) -> "Paths":
        return cls(resolve_home(home))

    def p(self, *parts: str) -> str:
        return os.path.join(self.home, *parts)

    @property
    def config(self): return self.p("config.json")
    @property
    def env_file(self): return self.p("env")
    @property
    def projects(self): return self.p("projects")
    @property
    def profiles(self): return self.p("profiles")
    @property
    def state(self): return self.p("state")
    @property
    def db(self): return self.p("state", "gsched.sqlite")
    @property
    def run(self): return self.p("run")
    @property
    def default_socket(self): return self.p("run", "gsched.sock")
    @property
    def pidfile(self): return self.p("run", "daemon.pid")
    @property
    def lockfile(self): return self.p("run", "daemon.lock")
    @property
    def jobs(self): return self.p("jobs")
    @property
    def checkpoints(self): return self.p("checkpoints")
    @property
    def logs(self): return self.p("logs")
    @property
    def log_file(self): return self.p("logs", "gsched.log")
    @property
    def venv(self): return self.p("venv")
    @property
    def app(self): return self.p("app")
    @property
    def bin(self): return self.p("bin")

    def job_dir(self, job_id: str) -> str:
        return self.p("jobs", job_id)

    def checkpoint_dir(self, job_id: str) -> str:
        return self.p("checkpoints", job_id)

    def socket(self, cfg_socket_path: str | None = None) -> str:
        return cfg_socket_path or self.default_socket

    def ensure_dirs(self) -> None:
        for d in (self.home, self.projects, self.profiles, self.state, self.run, self.jobs,
                  self.checkpoints, self.logs):
            mkdir_private(d)


def mkdir_private(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def check_socket_path(path: str) -> None:
    n = len(os.fsencode(path))
    if n >= SUN_PATH_MAX:
        raise PlatformError(
            f"socket path too long ({n} bytes); set socket_path in config or a shorter GSCHED_HOME")

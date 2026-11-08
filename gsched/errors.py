"""Error hierarchy and error codes (01 section 5)."""

from __future__ import annotations

# code -> CLI exit code
EXIT_CODES = {
    "schema": 4,
    "invalid": 4,
    "unknown_project": 2,
    "unknown_job": 2,
    "unknown_profile": 2,
    "forbidden": 2,
    "cwd_not_accessible": 2,
    "command_not_found": 2,
    "draining": 2,
    "too_big": 2,
    "bad_state": 2,
    "bad_lease": 2,
    "still_running": 2,
    "busy": 2,
    "internal": 4,
}


class GschedError(Exception):
    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        d = {"code": self.code, "message": self.message}
        if self.detail:
            d["detail"] = self.detail
        return d

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.code, 4)


class PlatformError(Exception):
    """The platform is unsupported or a probe failed its self-check. Daemon exits 78."""


class ConfigError(Exception):
    """Invalid configuration. Daemon exits 78. `key` names the offending key."""

    def __init__(self, key: str, message: str):
        super().__init__(f"config key {key!r}: {message}")
        self.key = key
        self.message = message


class StoreError(Exception):
    """Database problem (e.g. unsupported schema version). Daemon exits 78."""

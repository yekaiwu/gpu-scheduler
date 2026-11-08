"""Shared test helpers."""

from __future__ import annotations

from gsched.config import Config
from gsched.spec import SCHEMA_ID, parse_spec


def spec_dict(**over) -> dict:
    d = {"schema": SCHEMA_ID, "project": "p", "command": ["true"], "checkpoint": "atomic",
         "resources": {"memory_gib": 1, "gpu": "none"}, "runtime": {"estimate_s": 60}}
    for k, v in over.items():
        if k in ("resources", "runtime") and isinstance(v, dict):
            d[k] = {**d[k], **v}
        else:
            d[k] = v
    return d


def make_spec(cfg: Config | None = None, door="cli", **over):
    return parse_spec(spec_dict(**over), cfg or Config.defaults(), door=door)

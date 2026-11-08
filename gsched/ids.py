"""Job id and lease token generation (04 section 3)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time


def new_job_id(now_ms: int) -> str:
    return time.strftime("%Y%m%dT%H%M%S", time.localtime(now_ms / 1000)) + "-" + secrets.token_hex(2)


def new_lease() -> str:
    return secrets.token_hex(32)


def lease_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def lease_matches(token: str, stored_hash: str | None) -> bool:
    if not stored_hash or not isinstance(token, str):
        return False
    return hmac.compare_digest(lease_hash(token), stored_hash)

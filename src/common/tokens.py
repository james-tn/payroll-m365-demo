"""Signed JWT helper for CTA links and tokens.

Tokens carry purpose-scoped claims (approve / reject / handoff / discuss) and are
single-use (the audit log records jti consumption to prevent replay).
"""
from __future__ import annotations
import time
import uuid
from typing import Any, Optional

from jose import jwt, JWTError

from .config import get_settings
from .logging import get_logger

logger = get_logger(__name__)

_ALG = "HS256"
_used_jtis: set[str] = set()  # In-memory replay guard for the demo


class TokenError(Exception):
    pass


def mint(
    purpose: str,
    subject: str,
    extra: Optional[dict] = None,
    ttl_seconds: int = 3600,
) -> str:
    """Mint a short-lived signed token.

    purpose: e.g. "approve", "reject", "handoff", "discuss"
    subject: e.g. user email / persona / batch id
    extra: arbitrary claims to include
    """
    settings = get_settings()
    now = int(time.time())
    claims: dict[str, Any] = {
        "purpose": purpose,
        "sub": subject,
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": str(uuid.uuid4()),
    }
    if extra:
        claims.update(extra)
    return jwt.encode(claims, settings.cta_token_secret, algorithm=_ALG)


def verify(token: str, expected_purpose: Optional[str] = None, mark_used: bool = True) -> dict:
    """Verify signature, expiry, optional purpose; optionally mark jti consumed."""
    settings = get_settings()
    try:
        claims = jwt.decode(token, settings.cta_token_secret, algorithms=[_ALG])
    except JWTError as e:
        raise TokenError(f"invalid token: {e}") from e

    if expected_purpose and claims.get("purpose") != expected_purpose:
        raise TokenError(f"purpose mismatch: expected {expected_purpose}, got {claims.get('purpose')}")

    jti = claims.get("jti")
    if jti and jti in _used_jtis:
        raise TokenError("token already used")
    if mark_used and jti:
        _used_jtis.add(jti)

    return claims

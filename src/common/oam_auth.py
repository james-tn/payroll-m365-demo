"""Validate the bearer token that Outlook attaches to Action.Http calls.

Reference: https://learn.microsoft.com/en-us/outlook/actionable-messages/security-requirements

OAM tokens are JWTs signed by Microsoft's substrate service. To trust an inbound
Action.Http POST, we must:
  1. Verify the signature against the JWKS at
     https://substrate.office.com/sts/common/discovery/keys
  2. Check `iss == https://substrate.office.com/sts/`
  3. Check `aud == <the exact URL the action posted to>`
  4. Optionally check `sender == <expected sender email>` to make sure the
     click came from a card we actually sent.

The `sub` claim is the email of the person who clicked, which we use as the
approver identity for audit logs.
"""
from __future__ import annotations

import time
from typing import Optional

import httpx
from jose import jwt
from jose.exceptions import JWTError

SUBSTRATE_JWKS_URL = "https://substrate.office.com/sts/common/discovery/keys"
SUBSTRATE_ISSUER = "https://substrate.office.com/sts/"

_JWKS_TTL_SECONDS = 3600
_jwks_cache: dict = {"keys": None, "fetched_at": 0.0}


class OamAuthError(Exception):
    """Raised when an OAM bearer token fails validation."""


async def _fetch_jwks() -> dict:
    now = time.time()
    if _jwks_cache["keys"] is None or now - _jwks_cache["fetched_at"] > _JWKS_TTL_SECONDS:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(SUBSTRATE_JWKS_URL)
            r.raise_for_status()
            _jwks_cache["keys"] = r.json()
            _jwks_cache["fetched_at"] = now
    return _jwks_cache["keys"]


async def verify_oam_bearer(
    token: str,
    *,
    expected_audience: str,
    allowed_senders: Optional[set[str]] = None,
) -> dict:
    """Validate an OAM bearer token and return its claims.

    Raises OamAuthError on any failure (bad signature, wrong issuer, wrong audience,
    sender not in allow list, expired, etc.).
    """
    if not token:
        raise OamAuthError("missing token")

    jwks = await _fetch_jwks()

    try:
        claims = jwt.decode(
            token,
            jwks,
            algorithms=["RS256"],
            audience=expected_audience,
            issuer=SUBSTRATE_ISSUER,
            options={"verify_aud": True, "verify_iss": True, "verify_signature": True},
        )
    except JWTError as e:
        raise OamAuthError(f"jwt invalid: {e}")
    except Exception as e:
        raise OamAuthError(f"jwt validation error: {e}")

    if allowed_senders:
        sender = (claims.get("sender") or "").lower()
        allowed_lc = {s.lower() for s in allowed_senders if s}
        if sender and sender not in allowed_lc:
            raise OamAuthError(f"sender {sender!r} not in allow list")

    return claims

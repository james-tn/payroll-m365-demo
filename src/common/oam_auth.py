"""Validate the bearer token that Outlook attaches to Action.Http calls.

Reference: https://learn.microsoft.com/en-us/outlook/actionable-messages/enable-entra-token-for-actionable-messages

Microsoft is migrating Outlook Actionable Messages from legacy substrate
("External Access Tokens") to Entra-ID-issued tokens. New OAM providers must
use the Entra ID flow.

Token shape (Entra ID issued):
  iss   = https://login.microsoftonline.com/<tenant-id>/v2.0
  aud   = <AppIdUri> for your registered service (api://auth-am-.../...)
  tid   = tenant id of the user who clicked
  sub   = opaque user id
  preferred_username = user's UPN/email

To validate:
  1. Peek at unverified claims, read `tid`.
  2. Fetch JWKS for that tenant from the standard OIDC discovery doc.
  3. jwt.decode with audience=<AppIdUri>, issuer=https://login.microsoftonline.com/<tid>/v2.0.

We don't validate `sender` because Entra tokens don't always include it;
instead we rely on aud (only Outlook can mint a token with our AppIdUri as
audience, because the OAM service is pre-authorized to request it).
"""
from __future__ import annotations

import time
from typing import Optional

import httpx
from jose import jwt
from jose.exceptions import JWTError

_JWKS_TTL_SECONDS = 3600
_jwks_cache: dict[str, dict] = {}  # tenant_id -> {"keys": ..., "fetched_at": ...}


class OamAuthError(Exception):
    """Raised when an OAM bearer token fails validation."""


async def _fetch_tenant_jwks(tenant_id: str) -> dict:
    now = time.time()
    cached = _jwks_cache.get(tenant_id)
    if cached and now - cached["fetched_at"] < _JWKS_TTL_SECONDS:
        return cached["keys"]
    async with httpx.AsyncClient(timeout=10) as client:
        oid = await client.get(
            f"https://login.microsoftonline.com/{tenant_id}/v2.0/.well-known/openid-configuration"
        )
        oid.raise_for_status()
        jwks_uri = oid.json()["jwks_uri"]
        r = await client.get(jwks_uri)
        r.raise_for_status()
        keys = r.json()
    _jwks_cache[tenant_id] = {"keys": keys, "fetched_at": now}
    return keys


async def verify_oam_bearer(
    token: str,
    *,
    expected_audiences: list[str],
    allowed_tenants: Optional[set[str]] = None,
) -> dict:
    """Validate an Entra-ID-issued OAM bearer token and return its claims.

    Raises OamAuthError on any failure.

    `expected_audiences` is a list because v2.0 tokens use the App ID (GUID) as
    aud, while v1.0 tokens use the AppIdUri. We accept either so the demo works
    regardless of how the Entra app's requestedAccessTokenVersion is set.
    """
    if not token:
        raise OamAuthError("missing token")
    expected_audiences = [a for a in expected_audiences if a]
    if not expected_audiences:
        raise OamAuthError("server misconfigured: no expected audience")

    try:
        unverified = jwt.get_unverified_claims(token)
    except JWTError as e:
        raise OamAuthError(f"unparseable token: {e}")

    tid = unverified.get("tid")
    if not tid:
        raise OamAuthError("missing tid claim")
    if allowed_tenants and tid not in allowed_tenants:
        raise OamAuthError(f"tenant {tid} not in allow list")

    try:
        jwks = await _fetch_tenant_jwks(tid)
    except Exception as e:
        raise OamAuthError(f"jwks fetch failed for tenant {tid}: {e}")

    # v2.0 issuer adds "/v2.0"; v1.0 doesn't. Honor whatever ver the token claims.
    ver = (unverified.get("ver") or "2.0").strip()
    expected_iss = (
        f"https://login.microsoftonline.com/{tid}/v2.0"
        if ver.startswith("2") else
        f"https://sts.windows.net/{tid}/"
    )

    last_err: Optional[Exception] = None
    for aud in expected_audiences:
        try:
            return jwt.decode(
                token, jwks, algorithms=["RS256"], audience=aud, issuer=expected_iss,
                options={"verify_aud": True, "verify_iss": True, "verify_signature": True},
            )
        except JWTError as e:
            last_err = e
            continue
    raise OamAuthError(
        f"jwt invalid against any of {expected_audiences} (iss={expected_iss}, "
        f"token_aud={unverified.get('aud')!r}, token_iss={unverified.get('iss')!r}, "
        f"err={last_err})"
    )

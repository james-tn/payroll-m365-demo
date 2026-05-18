"""Proactive sender for Teams / Copilot.

Uses app-only Bot Framework credentials to:
  1. Acquire access token (client_credentials against botframework.com)
  2. Create a conversation (Copilot proactive thread or use stored Teams ref)
  3. Post the message activity carrying the Adaptive Card

For the demo: if we have a stored conversation reference, we use the SDK's
adapter.continue_conversation to post into that existing chat.
Otherwise, for Copilot, we hit the AgentProactive endpoint to spin up a new side thread.
"""
from __future__ import annotations
import json
import time
from typing import Any, Optional

import httpx

from ..common.config import get_settings
from ..common.logging import get_logger
from .conversation_store import StoredConversation

logger = get_logger(__name__)


_TOKEN_CACHE: dict[str, Any] = {"token": None, "exp": 0}


async def _get_app_token() -> str:
    """Get app-only token for the Bot Framework Connector API."""
    now = int(time.time())
    if _TOKEN_CACHE["token"] and _TOKEN_CACHE["exp"] > now + 60:
        return _TOKEN_CACHE["token"]

    s = get_settings()
    if not s.bot_app_id or not s.bot_app_password:
        raise RuntimeError("BOT_APP_ID / BOT_APP_PASSWORD not configured")

    # For SingleTenant apps, the token endpoint uses the tenant id, not botframework.com.
    # For multi-tenant bots, use botframework.com tenant.
    if s.bot_app_type.lower() == "singletenant" and s.bot_tenant_id and s.bot_tenant_id != "common":
        tenant = s.bot_tenant_id
        scope = f"{s.bot_app_id}/.default" if False else "https://api.botframework.com/.default"
    else:
        tenant = "botframework.com"
        scope = "https://api.botframework.com/.default"

    url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": s.bot_app_id,
        "client_secret": s.bot_app_password,
        "scope": scope,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, data=data)
        r.raise_for_status()
        body = r.json()
    _TOKEN_CACHE["token"] = body["access_token"]
    _TOKEN_CACHE["exp"] = now + int(body.get("expires_in", 3600))
    logger.info("acquired bot app token (expires in %ss)", body.get("expires_in"))
    return _TOKEN_CACHE["token"]


async def push_card_to_stored(stored: StoredConversation, card: dict, text: str = "") -> dict:
    """Push an Adaptive Card to an existing conversation we have a reference for."""
    token = await _get_app_token()
    ref = stored.conversation_reference
    service_url = ref.get("serviceUrl") or "https://smba.trafficmanager.net/teams/"
    conversation_id = (ref.get("conversation") or {}).get("id")
    if not conversation_id:
        raise RuntimeError("conversation reference has no conversation.id")

    activity = {
        "type": "message",
        "from": ref.get("bot", {}),
        "recipient": ref.get("user", {}),
        "conversation": {"id": conversation_id},
        "text": text,
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card,
            }
        ],
    }
    url = f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/activities"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            url,
            json=activity,
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code >= 300:
            logger.error("proactive push failed status=%d body=%s", r.status_code, r.text[:500])
            r.raise_for_status()
        logger.info("proactive card pushed conv=%s status=%d", conversation_id, r.status_code)
        try:
            return r.json()
        except Exception:
            return {"status": r.status_code}


async def create_copilot_proactive_thread(user_aad_object_id: str, tenant_id: str) -> dict:
    """Create a new Copilot proactive notification thread for a user.

    Returns the conversation dict with id + serviceUrl.
    """
    token = await _get_app_token()
    body = {
        "members": [{"id": user_aad_object_id}],
        "tenantId": tenant_id,
        "channelData": {
            "productContext": "Copilot",
            "conversation": {"conversationSubType": "AgentProactive"},
        },
    }
    url = "https://canary.botapi.skype.com/teams/v3/conversations"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
        if r.status_code >= 300:
            logger.error("create copilot thread failed status=%d body=%s", r.status_code, r.text[:500])
            r.raise_for_status()
        return r.json()

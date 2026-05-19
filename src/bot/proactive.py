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
    """Get app-only token for the Bot Framework Connector API.

    Supports three bot identity models:
    - UserAssignedMSI: federated identity via the User Assigned Managed Identity
      attached to this Container App. Recommended for multi-tenant ISV bots —
      Bot Framework now hard-deprecates classic multi-tenant client_secret bots.
    - SingleTenant: client_credentials at the bot's home tenant.
    - MultiTenant (legacy): client_credentials at botframework.com.
    """
    now = int(time.time())
    if _TOKEN_CACHE["token"] and _TOKEN_CACHE["exp"] > now + 60:
        return _TOKEN_CACHE["token"]

    s = get_settings()
    if not s.bot_app_id:
        raise RuntimeError("BOT_APP_ID not configured")

    app_type = (s.bot_app_type or "").lower()
    scope = "https://api.botframework.com/.default"

    if app_type == "userassignedmsi":
        # Acquire UAMI token directly from IMDS. The Container App injects
        # IDENTITY_ENDPOINT + IDENTITY_HEADER for managed identity access.
        import os
        identity_endpoint = os.environ.get("IDENTITY_ENDPOINT")
        identity_header = os.environ.get("IDENTITY_HEADER")
        if identity_endpoint and identity_header:
            params = {
                "api-version": "2019-08-01",
                "resource": "https://api.botframework.com",
                "client_id": s.bot_app_id,
            }
            headers = {"X-IDENTITY-HEADER": identity_header}
            url = identity_endpoint
        else:
            # IMDS fallback (e.g. VM, AKS)
            params = {
                "api-version": "2018-02-01",
                "resource": "https://api.botframework.com",
                "client_id": s.bot_app_id,
            }
            headers = {"Metadata": "true"}
            url = "http://169.254.169.254/metadata/identity/oauth2/token"

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, params=params, headers=headers)
            if r.status_code >= 300:
                logger.error("MSI token request failed status=%d body=%s", r.status_code, r.text[:500])
                r.raise_for_status()
            body = r.json()
        _TOKEN_CACHE["token"] = body["access_token"]
        # expires_on is unix epoch string for IDENTITY_ENDPOINT, expires_in seconds for IMDS
        if "expires_on" in body:
            _TOKEN_CACHE["exp"] = int(body["expires_on"])
        else:
            _TOKEN_CACHE["exp"] = now + int(body.get("expires_in", 3600))
        logger.info("acquired bot app token via UAMI (cached until %s)", _TOKEN_CACHE["exp"])
        return _TOKEN_CACHE["token"]

    if not s.bot_app_password:
        raise RuntimeError("BOT_APP_PASSWORD required for SingleTenant/MultiTenant bots")

    if app_type == "singletenant" and s.bot_tenant_id and s.bot_tenant_id != "common":
        tenant = s.bot_tenant_id
    else:
        tenant = "botframework.com"

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
    logger.info("acquired bot app token via client_credentials (expires in %ss)", body.get("expires_in"))
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


async def create_personal_chat(
    *,
    user_aad_object_id: str,
    user_display_name: str,
    tenant_id: str,
    service_url: Optional[str] = None,
) -> dict:
    """Create (or get) a 1:1 personal chat conversation between the bot and a user.

    Uses the Bot Framework Connector createConversation API:
        POST {serviceUrl}/v3/conversations
        body: { bot, isGroup=false, members=[{id: aadObjectId}], channelData.tenant.id }

    Returns a ConversationReference-shaped dict ready to be stashed in the
    ConversationStore and used with push_card_to_stored.

    Prerequisites:
      - The bot's Teams app MUST be installed in the user's personal scope
        (Teams → Apps → install). If not, createConversation returns 403.
      - A valid app-only Bot Framework token (UAMI / client_credentials).

    Raises RuntimeError with a helpful message on failure.
    """
    s = get_settings()
    if not s.bot_app_id:
        raise RuntimeError("BOT_APP_ID is required")
    if not user_aad_object_id:
        raise RuntimeError("user_aad_object_id is required")
    if not tenant_id:
        raise RuntimeError("tenant_id is required")

    base = (service_url or s.bot_service_url or "https://smba.trafficmanager.net/teams/").rstrip("/")
    url = f"{base}/v3/conversations"
    body = {
        "bot": {"id": f"28:{s.bot_app_id}"},
        "isGroup": False,
        "members": [{"id": user_aad_object_id}],
        "channelData": {"tenant": {"id": tenant_id}},
    }

    token = await _get_app_token()
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
        if r.status_code >= 300:
            excerpt = (r.text or "")[:400]
            if r.status_code == 403:
                raise RuntimeError(
                    f"createConversation 403 - the bot's Teams app is not installed in "
                    f"this user's personal scope. Install PayCycle in Teams for "
                    f"{user_display_name} once (Apps → search PayCycle → Add), then retry. "
                    f"({excerpt})"
                )
            raise RuntimeError(
                f"createConversation {r.status_code}: {excerpt}"
            )
        result = r.json()

    conv_id = result.get("id")
    if not conv_id:
        raise RuntimeError(f"createConversation returned no conversation id: {result}")

    logger.info(
        "auto-created Teams 1:1 chat conv=%s user=%s tenant=%s",
        conv_id[:24], user_display_name, tenant_id,
    )

    return {
        "channelId": "msteams",
        "user": {"id": user_aad_object_id, "name": user_display_name or user_aad_object_id,
                 "aadObjectId": user_aad_object_id},
        "bot": {"id": f"28:{s.bot_app_id}", "name": "PayCycle"},
        "conversation": {"id": conv_id, "tenantId": tenant_id},
        "serviceUrl": base + "/",
    }


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

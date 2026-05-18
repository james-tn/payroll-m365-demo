"""FastAPI app - bot endpoint + CTA endpoints + demo console.

The bot processes inbound activities using a minimal hand-rolled approach (we don't depend on
the full agents-aiohttp adapter to keep this demo small). For each activity we:
  1. Validate the JWT from Bot Framework (issuer/audience)
  2. Resolve the user persona
  3. Capture the conversation reference
  4. Route the user's message through the LangGraph agent
  5. Send the assistant's reply back via the Bot Framework Connector API

For invoke activities (Action.Execute from cards), we handle the verb and respond with a refresh card.
"""
from __future__ import annotations
import asyncio
import time
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jose import jwt

from .agent.graph import run_agent
from .bot.conversation_store import get_conversation_store
from .bot.proactive import _get_app_token, push_card_to_stored
from .cards.builders import (
    build_action_confirmation,
    build_admin_exception_notification,
    build_manager_approval_request,
    build_teams_continuation_card,
)
from .common.config import get_settings
from .common.logging import get_logger, init_logging
from .common.tokens import TokenError, mint, verify
from .demo_console.routes import router as demo_router
from .email_service.sender import send_email
from .flex.store import get_store

init_logging()
logger = get_logger(__name__)

app = FastAPI(title="PayCycle Payroll M365 Demo", version="0.1.0")
app.include_router(demo_router, prefix="/demo", tags=["demo"])


# ---- Health / root ----

@app.get("/")
async def root() -> dict:
    settings = get_settings()
    store = get_store()
    return {
        "service": "payroll-m365-demo",
        "status": "ok",
        "version": "0.1.0",
        "bot_app_id_configured": bool(settings.bot_app_id),
        "acs_configured": bool(settings.acs_connection_string),
        "openai_configured": bool(settings.azure_openai_api_key),
        "oam_originator_configured": bool(settings.oam_originator_id),
        "current_cycle": store.get_current_cycle()["id"],
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy"}


# ---- Bot Framework: /api/messages ----

_OPENID_KEYS: dict[str, Any] = {"keys": None, "fetched_at": 0}
_OIDC_CONFIG_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"

# Push dedup: {jti -> first_push_unix_ts}. TTL matches the token TTL (24h).
_PUSHED_JTIS: dict[str, float] = {}
_PUSH_DEDUP_TTL_SECONDS = 86400

# Prefetcher User-Agent signatures (Defender Safe Links, Outlook link preview, etc.).
_PREFETCHER_UA_MARKERS = (
    "BingPreview",
    "MicrosoftPreview",
    "Microsoft Office",
    "Microsoft-WebDAV",
    "OutlookConnector",
    "SkypeUriPreview",
    "TeamsLinkPreview",
    "facebookexternalhit",
    "Slackbot-LinkExpanding",
    "Twitterbot",
    "LinkedInBot",
    "ms-office",
    "Mozilla/4.0 (compatible; ms-office;",
)


def _looks_like_prefetcher(user_agent: str) -> bool:
    if not user_agent:
        # No UA at all → very likely an automated probe, not a human browser
        return True
    ua = user_agent.lower()
    for marker in _PREFETCHER_UA_MARKERS:
        if marker.lower() in ua:
            return True
    return False


def _push_already_done(jti: str) -> bool:
    """Return True if we've already pushed for this token id (within TTL)."""
    now = time.time()
    # Opportunistic GC of stale entries
    if len(_PUSHED_JTIS) > 1000:
        cutoff = now - _PUSH_DEDUP_TTL_SECONDS
        for k in [k for k, v in _PUSHED_JTIS.items() if v < cutoff]:
            _PUSHED_JTIS.pop(k, None)
    ts = _PUSHED_JTIS.get(jti)
    return bool(ts and (now - ts) < _PUSH_DEDUP_TTL_SECONDS)


def _mark_push_done(jti: str) -> None:
    _PUSHED_JTIS[jti] = time.time()


async def _get_signing_keys() -> list[dict]:
    """Fetch and cache Bot Framework signing keys (refresh every 24h)."""
    now = time.time()
    if _OPENID_KEYS["keys"] and now - _OPENID_KEYS["fetched_at"] < 86400:
        return _OPENID_KEYS["keys"]
    async with httpx.AsyncClient(timeout=10) as client:
        cfg = (await client.get(_OIDC_CONFIG_URL)).json()
        jwks = (await client.get(cfg["jwks_uri"])).json()
    _OPENID_KEYS["keys"] = jwks["keys"]
    _OPENID_KEYS["fetched_at"] = now
    return jwks["keys"]


async def _validate_bot_jwt(auth_header: Optional[str]) -> dict:
    """Validate the inbound Bearer token from Bot Framework. Returns claims."""
    settings = get_settings()
    if not auth_header or not auth_header.lower().startswith("bearer "):
        logger.warning("jwt: missing or malformed Authorization header (got=%r)", auth_header[:40] if auth_header else None)
        raise HTTPException(401, "missing bearer token")
    token = auth_header.split(" ", 1)[1]
    try:
        unverified = jwt.get_unverified_header(token)
        unverified_claims = jwt.get_unverified_claims(token)
        kid = unverified.get("kid")
        alg = unverified.get("alg", "RS256")
        iss = unverified_claims.get("iss")
        aud = unverified_claims.get("aud")
        logger.info("jwt: header kid=%s alg=%s | claims iss=%s aud=%s expected_aud=%s",
                    kid, alg, iss, aud, settings.bot_app_id)
        keys = await _get_signing_keys()
        key = next((k for k in keys if k.get("kid") == kid), None)
        if not key:
            logger.warning("jwt: unknown signing key kid=%s (have %d keys from BF OIDC)", kid, len(keys))
            raise HTTPException(401, "unknown signing key")
        claims = jwt.decode(
            token,
            key,
            algorithms=[alg],
            audience=settings.bot_app_id,
            options={"verify_iss": False},
        )
        logger.info("jwt: validated ok, claims=%s", {k: claims.get(k) for k in ("iss", "aud", "appid", "serviceurl")})
        return claims
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("jwt: validation failed: %s: %s", type(e).__name__, e)
        raise HTTPException(401, f"invalid token: {e}") from e


@app.post("/api/messages")
async def messages(request: Request) -> Response:
    """Bot Framework activity endpoint."""
    settings = get_settings()
    # Skip JWT validation if bot app id not configured (local dev / unit tests)
    if settings.bot_app_id:
        await _validate_bot_jwt(request.headers.get("Authorization"))
    activity = await request.json()
    activity_type = activity.get("type", "")
    logger.info("inbound activity type=%s channel=%s", activity_type, activity.get("channelId"))

    if activity_type == "conversationUpdate":
        # User added or bot added - welcome card
        return await _handle_conversation_update(activity)
    if activity_type == "message":
        return await _handle_message(activity)
    if activity_type == "invoke":
        return await _handle_invoke(activity)
    return JSONResponse({"status": "ignored"}, status_code=200)


async def _resolve_persona(activity: dict) -> str:
    """Resolve which persona this conversation belongs to.

    Demo: peek at conversation_store for pending_context with explicit persona;
    otherwise default to payroll_admin.
    """
    settings = get_settings()
    # If user has a pending handoff context from a CTA link, use the persona it specified.
    store = get_conversation_store()
    email = (activity.get("from") or {}).get("name", "") or settings.demo_user_email
    # Check both personas for a pending context
    for persona in ("payroll_manager", "payroll_admin"):
        sc = store.get_by_user(email, persona)
        if sc and sc.pending_context:
            return persona
    return "payroll_admin"


async def _handle_conversation_update(activity: dict) -> Response:
    settings = get_settings()
    members_added = activity.get("membersAdded") or []
    bot_id = (activity.get("recipient") or {}).get("id", "")
    # Capture the ConversationReference even before the user types - this lets later
    # proactive pushes (e.g. from an email handoff) find the chat.
    conv_store = get_conversation_store()
    for persona in ("payroll_admin", "payroll_manager"):
        try:
            stored = conv_store.upsert_from_activity(activity, persona=persona)
            conv_store.alias_to_emails(
                stored, [settings.demo_admin_email, settings.demo_manager_email]
            )
        except Exception as e:
            logger.warning("failed to capture conversation ref on update: %s", e)
    for m in members_added:
        if m.get("id") != bot_id:
            # Send welcome
            welcome_card = {
                "type": "AdaptiveCard",
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "version": "1.5",
                "body": [
                    {"type": "TextBlock", "text": "👋 PayCycle Payroll Assistant", "size": "Large", "weight": "Bolder", "wrap": True},
                    {"type": "TextBlock", "text": "I help you review payroll exceptions, prepare batches for approval, and answer payroll-cycle questions.", "wrap": True},
                    {"type": "TextBlock", "text": "Ask me things like:", "weight": "Bolder", "spacing": "Medium"},
                    {"type": "TextBlock", "text": "• Show me open exceptions in the current cycle\n• Why is Joseph Smith's overtime so high?\n• Summarize the current payroll batch", "wrap": True},
                ],
            }
            await _reply_to_activity(activity, [welcome_card], "Welcome to PayCycle.")
    return JSONResponse({"status": "ok"}, status_code=200)


async def _handle_message(activity: dict) -> Response:
    settings = get_settings()
    persona = await _resolve_persona(activity)
    store = get_conversation_store()
    stored = store.upsert_from_activity(activity, persona=persona)
    # Also alias under the configured demo email(s) for the OTHER persona so that
    # an email handoff sent under a different persona key can find this chat.
    store.alias_to_emails(stored, [settings.demo_admin_email, settings.demo_manager_email])

    # If user has a pending context from email CTA, fold it in as extra_context for one turn.
    pending = store.consume_pending_context(stored.user_email, persona)
    if pending is None:
        # Also check the canonical demo email keys
        for em in (settings.demo_admin_email, settings.demo_manager_email):
            if em:
                pending = store.consume_pending_context(em, persona) or pending

    conv_id = (activity.get("conversation") or {}).get("id", "session")
    user_text = activity.get("text") or "Hello"

    try:
        reply_text = await run_agent(
            user_message=user_text,
            session_id=conv_id,
            persona=persona,
            extra_context=pending,
        )
    except Exception as e:
        logger.exception("agent failure: %s", e)
        reply_text = f"Sorry, I hit an error while looking that up. ({type(e).__name__})"

    await _reply_to_activity(activity, [], reply_text)
    return JSONResponse({"status": "ok"}, status_code=200)


async def _handle_invoke(activity: dict) -> Response:
    """Handle Action.Execute callbacks from Adaptive Cards in Teams."""
    name = activity.get("name", "")
    if name != "adaptiveCard/action":
        return JSONResponse({"status": "unhandled"}, status_code=200)

    value = activity.get("value") or {}
    action_data = (value.get("action") or {}).get("data") or {}
    verb = action_data.get("verb", "")
    logger.info("invoke verb=%s data=%s", verb, action_data)

    if verb == "approve_batch":
        batch_id = action_data.get("batch_id", "")
        try:
            batch = get_store().approve_batch(batch_id, approved_by=action_data.get("approver", "manager"))
            card = build_action_confirmation(
                title="✅ Batch approved",
                message=f"Batch {batch['id']} approved at {batch['approved_at']}.",
                sub=f"{batch['totals']['employees']} employees · ${batch['totals']['gross']:,.2f} gross",
            )
            return JSONResponse({
                "statusCode": 200,
                "type": "application/vnd.microsoft.card.adaptive",
                "value": card,
            })
        except (KeyError, ValueError) as e:
            card = build_action_confirmation(title="Unable to approve", message=str(e), style="error")
            return JSONResponse({
                "statusCode": 200,
                "type": "application/vnd.microsoft.card.adaptive",
                "value": card,
            })

    if verb == "ask":
        card = build_action_confirmation(
            title="Type your question",
            message="Just type your question in the chat below and I'll dig in.",
            style="info",
        )
        return JSONResponse({
            "statusCode": 200,
            "type": "application/vnd.microsoft.card.adaptive",
            "value": card,
        })

    card = build_action_confirmation(
        title="Action received",
        message=f"Got it: {verb}",
        style="info",
    )
    return JSONResponse({
        "statusCode": 200,
        "type": "application/vnd.microsoft.card.adaptive",
        "value": card,
    })


async def _reply_to_activity(activity: dict, cards: list[dict], text: str = "") -> None:
    """Send a reply back via the Bot Framework Connector REST API."""
    service_url = activity.get("serviceUrl", "").rstrip("/")
    conversation_id = (activity.get("conversation") or {}).get("id", "")
    if not service_url or not conversation_id:
        logger.warning("cannot reply - missing serviceUrl/conversation.id")
        return

    token = await _get_app_token()
    payload: dict[str, Any] = {
        "type": "message",
        "from": activity.get("recipient", {}),
        "recipient": activity.get("from", {}),
        "conversation": {"id": conversation_id},
        "replyToId": activity.get("id"),
        "text": text,
    }
    if cards:
        payload["attachments"] = [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": c} for c in cards
        ]
    url = f"{service_url}/v3/conversations/{conversation_id}/activities/{activity.get('id', '')}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=payload, headers={"Authorization": f"Bearer {token}"})
        if r.status_code >= 300:
            logger.error("reply failed status=%d body=%s", r.status_code, r.text[:300])


# ---- CTA endpoints (called by Outlook actionable card buttons + handoff redirects) ----

def _outlook_card_response(card: dict) -> Response:
    """Return the special CARD-UPDATE-IN-BODY response that refreshes the card inline in Outlook."""
    headers = {
        "CARD-UPDATE-IN-BODY": "true",
        "Content-Type": "application/json",
    }
    return JSONResponse(content=card, headers=headers)


@app.post("/cta/approve")
async def cta_approve(request: Request) -> Response:
    token = request.query_params.get("token", "")
    try:
        claims = verify(token, expected_purpose="approve")
    except TokenError as e:
        return _outlook_card_response(build_action_confirmation(
            title="Link expired", message=str(e), style="error"
        ))
    batch_id = claims.get("batch_id", "")
    try:
        batch = get_store().approve_batch(batch_id, approved_by=claims.get("sub", "manager"))
    except (KeyError, ValueError) as e:
        return _outlook_card_response(build_action_confirmation(
            title="Unable to approve", message=str(e), style="error"
        ))
    # Confirmation card + (later) push notification back to admin
    card = build_action_confirmation(
        title="✅ Payroll approved",
        message=f"Batch {batch_id} approved by {claims.get('sub')} at {batch['approved_at']}.",
        sub=f"{batch['totals']['employees']} employees · ${batch['totals']['gross']:,.2f} gross will pay on the scheduled date.",
    )
    # Notify the admin (best-effort)
    asyncio.create_task(_notify_admin_of_approval(batch))
    return _outlook_card_response(card)


@app.post("/cta/reject")
async def cta_reject(request: Request) -> Response:
    token = request.query_params.get("token", "")
    try:
        claims = verify(token, expected_purpose="reject")
    except TokenError as e:
        return _outlook_card_response(build_action_confirmation(
            title="Link expired", message=str(e), style="error"
        ))
    body = await request.json()
    reason = (body or {}).get("reason", "").strip() or "(no reason provided)"
    batch_id = claims.get("batch_id", "")
    try:
        batch = get_store().reject_batch(batch_id, rejected_by=claims.get("sub", "manager"), reason=reason)
    except (KeyError, ValueError) as e:
        return _outlook_card_response(build_action_confirmation(
            title="Unable to reject", message=str(e), style="error"
        ))
    card = build_action_confirmation(
        title="❌ Payroll rejected",
        message=f"Batch {batch_id} rejected. Reason sent to Payroll Admin.",
        sub=reason,
        style="warning",
    )
    return _outlook_card_response(card)


@app.get("/cta/handoff")
async def cta_handoff(request: Request) -> Response:
    """User clicked 'Discuss in Teams/Copilot' in Outlook.

    Steps:
      1. Validate one-time token
      2. Look up stored conversation reference for this user+persona+surface
      3. Push a context-laden Adaptive Card into that conversation (proactive)
      4. Redirect to the Teams/Copilot deep link that opens that chat
      5. User lands in Teams with the card at the top
    """
    token = request.query_params.get("token", "")
    surface = request.query_params.get("surface", "teams")
    try:
        claims = verify(token, expected_purpose="handoff", mark_used=False)
    except TokenError as e:
        return HTMLResponse(_simple_error_page("Link expired or invalid", str(e)), status_code=400)

    settings = get_settings()
    persona = claims.get("persona", "payroll_admin")
    user_email = claims.get("sub", settings.demo_user_email)
    batch_id = claims.get("batch_id", "BATCH-2026-05B")
    intent = claims.get("intent", "discuss")

    # Build the context card
    store = get_store()
    batch = store.get_batch(batch_id)
    if not batch:
        return HTMLResponse(_simple_error_page("Batch not found", batch_id), status_code=404)
    facts = [
        {"title": "Batch", "value": batch["id"]},
        {"title": "Cycle", "value": batch["cycle_label"]},
        {"title": "Status", "value": batch["status"].title()},
        {"title": "Employees", "value": str(batch["totals"]["employees"])},
        {"title": "Gross", "value": f"${batch['totals']['gross']:,.2f}"},
    ]
    summary = (
        "I've loaded the batch details. Ask me anything about the exceptions, "
        "the totals, or any employee's history. I have the full payroll context for this cycle."
    )
    primary_label = None
    primary_data = None
    if persona == "payroll_manager" and batch["status"] == "submitted":
        primary_label = "✅ Approve this batch"
        primary_data = {"verb": "approve_batch", "batch_id": batch_id, "approver": user_email}

    card = build_teams_continuation_card(
        title=f"Payroll context · {batch['cycle_label']}",
        summary=summary,
        facts=facts,
        base_url=settings.app_base_url,
        primary_action_label=primary_label,
        primary_action_data=primary_data,
    )

    # Attempt proactive push to the stored conversation - but DEDUP first.
    # The same handoff URL is GETted multiple times by Defender Safe Links scrubbers,
    # Outlook link prefetch, and browser pre-render before the user actually clicks.
    # Each token's jti is unique per email; push at most once per jti within the TTL.
    conv_store = get_conversation_store()
    stored = conv_store.get_by_user(user_email, persona)
    pushed = False
    already = False
    jti = claims.get("jti", "")
    user_agent = request.headers.get("user-agent", "")
    is_prefetcher = _looks_like_prefetcher(user_agent)
    if jti and _push_already_done(jti):
        already = True
        logger.info("handoff: skipping duplicate push jti=%s ua=%r", jti, user_agent[:80])
    elif is_prefetcher:
        logger.info("handoff: skipping push for prefetcher ua=%r", user_agent[:80])
    elif stored and stored.conversation_reference:
        try:
            await push_card_to_stored(stored, card, text="Continuing from your email...")
            pushed = True
            if jti:
                _mark_push_done(jti)
        except Exception as e:
            logger.warning("proactive push failed: %s", e)

    # Also set pending_context for the canonical demo emails (the JWT 'sub' may not
    # exactly match what Teams sends us as the user identity).
    for em in (settings.demo_admin_email, settings.demo_manager_email):
        if em:
            conv_store.set_pending_context(em, persona, {
                "batch_id": batch_id,
                "intent": intent,
                "from_email_link": True,
                "source_event": claims.get("event", ""),
            })
    # Set pending context so the agent's first turn in this chat knows what we're discussing
    conv_store.set_pending_context(user_email, persona, {
        "batch_id": batch_id,
        "intent": intent,
        "from_email_link": True,
        "source_event": claims.get("event", ""),
    })

    # Redirect to the Teams deep link
    deep_link = f"https://teams.microsoft.com/l/chat/0/0?users=28:{settings.bot_app_id}"
    if surface == "copilot":
        deep_link = "https://m365.cloud.microsoft/chat"

    # For prefetchers, return a minimal HTML body without redirect so we don't
    # influence preview rendering. Defender/Outlook only need a 200.
    if is_prefetcher:
        return HTMLResponse("<!doctype html><title>PayCycle handoff</title>", status_code=200)

    # If a real user click but no conv ref (or push failed and not already done), show interstitial.
    if not pushed and not already:
        html = _simple_redirect_page(
            "Opening PayCycle in Teams...",
            f"It looks like you haven't talked with the PayCycle agent in {surface.title()} yet. "
            f"It will open in a moment - just say hi and the agent will pick up the context.",
            deep_link,
        )
        return HTMLResponse(html, status_code=200)

    return RedirectResponse(deep_link, status_code=302)


async def _notify_admin_of_approval(batch: dict) -> None:
    """Send a confirmation email to the admin after manager approves."""
    settings = get_settings()
    if not settings.acs_sender_address:
        return
    confirmation_card = build_action_confirmation(
        title="✅ Your batch was approved",
        message=f"{batch['approved_by']} approved {batch['id']} ({batch['cycle_label']}).",
        sub=f"Pay run will execute on the scheduled date. {batch['totals']['employees']} employees · "
            f"${batch['totals']['gross']:,.2f} gross.",
    )
    try:
        await send_email(
            to=settings.demo_user_email,
            subject=f"[PayCycle] Batch {batch['id']} approved",
            card=confirmation_card,
            fallback_text=f"Batch {batch['id']} was approved by {batch['approved_by']}.",
        )
    except Exception as e:
        logger.warning("admin notification email failed: %s", e)


# ---- HTML helpers ----

def _simple_error_page(title: str, detail: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font-family:Segoe UI,Arial;max-width:520px;margin:80px auto;padding:24px;color:#333}}</style>
</head><body><h2 style="color:#d13438">{title}</h2><p>{detail}</p></body></html>"""


def _simple_redirect_page(title: str, message: str, url: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{title}</title>
<meta http-equiv="refresh" content="3;url={url}">
<style>body{{font-family:Segoe UI,Arial;max-width:520px;margin:80px auto;padding:24px;color:#333;text-align:center}}</style>
</head><body><h2 style="color:#2667ff">{title}</h2><p>{message}</p>
<p><a href="{url}" style="background:#2667ff;color:#fff;padding:10px 18px;border-radius:6px;text-decoration:none">
Open now</a></p></body></html>"""

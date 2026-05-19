"""Demo console - lightweight web UI for triggering scenarios during a live demo."""
from __future__ import annotations
import asyncio
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..bot.conversation_store import get_conversation_store
from ..bot.proactive import push_card_to_stored
from ..cards.builders import (
    build_admin_exception_notification,
    build_exception_worklist_card,
    build_manager_approval_request,
    build_teams_continuation_card,
)
from ..common.config import get_settings
from ..common.logging import get_logger
from ..common.tokens import mint
from ..email_service.sender import send_email
from ..email_service.templates import (
    render_admin_exception_email,
    render_manager_approval_email,
)
from ..flex.store import get_store

router = APIRouter()
logger = get_logger(__name__)


_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>PayCycle Demo Console</title>
  <style>
    body {{ font-family: 'Segoe UI', Arial, sans-serif; max-width: 920px; margin: 32px auto; padding: 24px; color: #222; }}
    h1 {{ color: #2667ff; margin-bottom: 4px; }}
    h2 {{ margin-top: 32px; border-bottom: 1px solid #eee; padding-bottom: 6px; }}
    .step {{ background: #f6f8fa; padding: 18px 22px; border-radius: 8px; margin-bottom: 16px; border-left: 4px solid #2667ff; }}
    .step h3 {{ margin: 0 0 8px; font-size: 16px; }}
    .step p {{ margin: 6px 0; font-size: 14px; color: #555; }}
    button {{ background: #2667ff; color: white; border: 0; padding: 10px 18px; border-radius: 6px; cursor: pointer; font-size: 14px; }}
    button:hover {{ background: #1d4fc4; }}
    .secondary {{ background: #586069; }}
    .state-table {{ width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 14px; }}
    .state-table th, .state-table td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid #eee; }}
    .state-table th {{ background: #f6f8fa; }}
    .status {{ font-weight: bold; padding: 2px 8px; border-radius: 4px; font-size: 12px; }}
    .status-draft {{ background: #e8eef7; color: #2667ff; }}
    .status-submitted {{ background: #fff4ce; color: #806000; }}
    .status-approved {{ background: #dff6dd; color: #107c10; }}
    .status-rejected {{ background: #fde7e9; color: #a4262c; }}
    .meta {{ color: #888; font-size: 12px; }}
    form {{ display: inline; }}
    .pill {{ display: inline-block; background: #eef; padding: 2px 8px; border-radius: 10px; font-size: 11px; color: #2667ff; }}
    .flash {{ background: #dff6dd; color: #107c10; padding: 10px 16px; border-radius: 6px; margin: 8px 0; white-space: pre-wrap; }}
    .delivery-mode {{ display: inline-flex; gap: 14px; margin: 8px 0 12px; padding: 8px 12px; background: #eef3ff; border-radius: 6px; font-size: 13px; }}
    .delivery-mode label {{ cursor: pointer; }}
    .delivery-mode input {{ vertical-align: middle; margin-right: 4px; }}
  </style>
</head>
<body>
  <h1>PayCycle Demo Console</h1>
  <p class="meta">Multi-tenant payroll ISV scenario - proactive notification, Outlook actionable email, Teams handoff</p>
  {flash}

  <h2>Current state</h2>
  <table class="state-table">
    <tr><th>Item</th><th>Value</th></tr>
    <tr><td>Company</td><td>{company_name}</td></tr>
    <tr><td>Pay cycle</td><td>{cycle_label} (id: {cycle_id})</td></tr>
    <tr><td>Cycle deadline</td><td>{cycle_deadline}</td></tr>
    <tr><td>Demo user (both personas)</td><td>{demo_email}</td></tr>
    <tr><td>Email sender</td><td>{sender}</td></tr>
    <tr><td>OAM originator registered</td><td>{oam_status}</td></tr>
    <tr><td>Teams conv ref (admin persona)</td><td>{teams_admin_status}</td></tr>
    <tr><td>Teams conv ref (manager persona)</td><td>{teams_manager_status}</td></tr>
  </table>

  <h2>Payroll batch state</h2>
  <table class="state-table">
    <tr><th>Batch</th><th>Status</th><th>Submitted by</th><th>Approved by</th></tr>
    {batch_rows}
  </table>

  <h2>Open exceptions</h2>
  <table class="state-table">
    <tr><th>ID</th><th>Employee</th><th>Category</th><th>Impact</th><th>Status</th></tr>
    {exception_rows}
  </table>

  <h2>Demo steps</h2>

  <div class="step">
    <h3>Step 1 - Notify Payroll Admin of open exceptions</h3>
    <p>Plays the <span class="pill">payroll_admin</span> persona (Maria) being notified that {open_count}
    exception{open_plural} need review for cycle <b>{cycle_label}</b>.</p>
    <form method="post" action="/demo/send-admin-alert">
      <div class="delivery-mode">
        <span><b>Deliver via:</b></span>
        <label><input type="radio" name="delivery_mode" value="email" checked>📧 Email (ACS → Outlook)</label>
        <label><input type="radio" name="delivery_mode" value="teams">💬 Teams (proactive)</label>
        <label><input type="radio" name="delivery_mode" value="both">📧+💬 Both</label>
      </div>
      <br>
      <button type="submit">Send admin notification</button>
    </form>
  </div>

  <div class="step">
    <h3>Step 2 - Admin submits batch for approval</h3>
    <p>Simulates Maria resolving exceptions and submitting batch <b>BATCH-2026-05B</b>. Sends an approval
    request to the Payroll Manager (David) via the chosen channel.</p>
    <form method="post" action="/demo/submit-batch">
      <input type="hidden" name="batch_id" value="BATCH-2026-05B">
      <div class="delivery-mode">
        <span><b>Deliver via:</b></span>
        <label><input type="radio" name="delivery_mode" value="email" checked>📧 Email (ACS → Outlook)</label>
        <label><input type="radio" name="delivery_mode" value="teams">💬 Teams (proactive)</label>
        <label><input type="radio" name="delivery_mode" value="both">📧+💬 Both</label>
      </div>
      <br>
      <button type="submit">Submit batch &amp; notify approver</button>
    </form>
  </div>

  <div class="step">
    <h3>Step 3 - (in your inbox / Teams chat) Take action</h3>
    <p>If you chose email: inline Approve / Flag buttons fire silently in Outlook for clients that support
    actionable messages; the "Review with PayCycle Assistant" button always works for handoff.<br>
    If you chose Teams: the worklist card lands directly in your bot chat with Action.Execute buttons.
    No client setup required.</p>
  </div>

  <div class="step">
    <h3>Reset demo state</h3>
    <p>Resets all mock data back to the initial draft state (exceptions reopened, batch back to draft, audit log cleared).</p>
    <form method="post" action="/demo/reset">
      <button type="submit" class="secondary">Reset</button>
    </form>
  </div>

  <h2>Audit log</h2>
  <table class="state-table">
    <tr><th>At</th><th>Event</th><th>Payload</th></tr>
    {audit_rows}
  </table>
</body>
</html>
"""


def _conv_ref_status(persona: str) -> str:
    s = get_settings()
    conv_store = get_conversation_store()
    for em in (s.demo_user_email, s.demo_admin_email, s.demo_manager_email):
        if not em:
            continue
        stored = conv_store.get_by_user(em, persona)
        if stored and stored.conversation_reference.get("conversation", {}).get("id"):
            return f"✅ {stored.surface} ({em})"
    return "❌ none (say hi to the bot first for proactive delivery)"


def _render(flash: str = "") -> str:
    s = get_settings()
    store = get_store()
    company = store.get_company()
    cycle = store.get_current_cycle()

    batch_rows = []
    for batch_id in ("BATCH-2026-05B",):
        b = store.get_batch(batch_id)
        if not b:
            continue
        status_class = f"status-{b['status']}"
        batch_rows.append(
            f"<tr><td>{b['id']}</td>"
            f"<td><span class='status {status_class}'>{b['status'].upper()}</span></td>"
            f"<td>{b.get('submitted_by') or '—'}</td>"
            f"<td>{b.get('approved_by') or b.get('rejected_by') or '—'}</td></tr>"
        )

    open_exceptions = store.list_open_exceptions()
    exception_rows = []
    for e in open_exceptions:
        exception_rows.append(
            f"<tr><td>{e['id']}</td>"
            f"<td>{e['employee_name']}</td>"
            f"<td>{e['category']}</td>"
            f"<td>${e['amount_impact']:,.2f}</td>"
            f"<td>{e['status']}</td></tr>"
        )
    if not exception_rows:
        exception_rows.append("<tr><td colspan='5' class='meta'>No open exceptions.</td></tr>")

    audit_rows = []
    for ev in reversed(store.get_audit_log()[-10:]):
        audit_rows.append(
            f"<tr><td>{ev['at'].split('.')[0]}Z</td>"
            f"<td>{ev['type']}</td>"
            f"<td><code style='font-size:11px'>{ev['payload']}</code></td></tr>"
        )
    if not audit_rows:
        audit_rows.append("<tr><td colspan='3' class='meta'>No events yet.</td></tr>")

    return _HTML_TEMPLATE.format(
        company_name=company["name"],
        cycle_label=cycle["label"],
        cycle_id=cycle["id"],
        cycle_deadline=cycle["deadline"],
        demo_email=s.demo_user_email,
        sender=s.acs_sender_address or "(not configured)",
        oam_status="✅ yes" if s.oam_originator_id else "❌ no (action buttons disabled in Outlook)",
        teams_admin_status=_conv_ref_status("payroll_admin"),
        teams_manager_status=_conv_ref_status("payroll_manager"),
        open_count=len(open_exceptions),
        open_plural="s" if len(open_exceptions) != 1 else "",
        batch_rows="".join(batch_rows) or "<tr><td colspan='4' class='meta'>No batches.</td></tr>",
        exception_rows="".join(exception_rows),
        audit_rows="".join(audit_rows),
        flash=f"<div class='flash'>{flash}</div>" if flash else "",
    )


@router.get("/console", response_class=HTMLResponse)
async def console(request: Request) -> HTMLResponse:
    flash = request.query_params.get("flash", "")
    return HTMLResponse(_render(flash))


# ---- Artifact builders (shared between delivery channels) ----


def _build_admin_artifacts() -> dict[str, Any]:
    s = get_settings()
    store = get_store()
    company = store.get_company()
    cycle = store.get_current_cycle()
    exceptions = store.list_open_exceptions()
    if not exceptions:
        return {"empty": True}

    handoff_token = mint(
        purpose="handoff",
        subject=s.demo_user_email,
        extra={
            "persona": "payroll_admin",
            "batch_id": "BATCH-2026-05B",
            "intent": "review_exceptions",
            "event": f"alert/{cycle['id']}",
            "exception_ids": [e["id"] for e in exceptions],
        },
        ttl_seconds=86400,
    )

    inline_card = build_admin_exception_notification(
        company_name=company["name"],
        cycle_label=cycle["label"],
        deadline_iso=cycle["deadline"],
        exceptions=exceptions,
        base_url=s.app_base_url,
        discuss_token=handoff_token,
    )

    html_body, plain_text = render_admin_exception_email(
        admin_name="Maria",
        company_name=company["name"],
        cycle_label=cycle["label"],
        deadline_iso=cycle["deadline"],
        exceptions=exceptions,
        handoff_token=handoff_token,
        base_url=s.app_base_url,
        total_employees=cycle.get("employees_included", 0),
        total_gross=cycle.get("estimated_gross", 0),
        inline_card=inline_card,
    )

    return {
        "empty": False,
        "subject": f"🔔 {len(exceptions)} payroll exception{'s' if len(exceptions) != 1 else ''} need review · {cycle['label']}",
        "html_body": html_body,
        "plain_text": plain_text,
        "inline_card": inline_card,
        "handoff_token": handoff_token,
        "exception_ids": [e["id"] for e in exceptions],
        "batch_id": "BATCH-2026-05B",
        "exceptions": exceptions,
        "company": company,
        "cycle": cycle,
    }


def _build_manager_artifacts(batch_id: str) -> dict[str, Any]:
    s = get_settings()
    store = get_store()
    company = store.get_company()

    # Simulate Maria having resolved the exceptions
    for exc in store.list_open_exceptions():
        store.resolve_exception(
            exc["id"],
            resolver="Maria Hernandez (Payroll Admin)",
            notes=f"Reviewed and approved by admin on {datetime.utcnow().isoformat()}Z",
        )

    batch = store.submit_batch(
        batch_id,
        submitted_by="Maria Hernandez (Payroll Admin)",
        admin_notes=(
            "Joseph Smith's 14h overtime is legitimate - confirmed assignment to Project Atlas tooling rebuild. "
            "Sarah Lee's PTO of 6h approved verbally by Daniel Cruz this morning; PTO-9821 will be updated in Flex by EOD."
        ),
    )
    exception_count = len(store.list_exceptions_for_batch(batch_id))

    approve_token = mint(
        purpose="approve",
        subject=s.demo_user_email,
        extra={"batch_id": batch_id, "persona": "payroll_manager"},
        ttl_seconds=7 * 24 * 3600,
    )
    reject_token = mint(
        purpose="reject",
        subject=s.demo_user_email,
        extra={"batch_id": batch_id, "persona": "payroll_manager"},
        ttl_seconds=7 * 24 * 3600,
    )
    handoff_token = mint(
        purpose="handoff",
        subject=s.demo_user_email,
        extra={
            "persona": "payroll_manager",
            "batch_id": batch_id,
            "intent": "review_for_approval",
            "event": f"approval/{batch_id}",
        },
        ttl_seconds=7 * 24 * 3600,
    )

    inline_card = build_manager_approval_request(
        company_name=company["name"],
        cycle_label=batch["cycle_label"],
        batch_id=batch_id,
        submitted_by="Maria Hernandez",
        totals=batch["totals"],
        exception_count=exception_count,
        admin_notes=batch["admin_notes"],
        base_url=s.app_base_url,
        approve_token=approve_token,
        reject_token=reject_token,
        discuss_token=handoff_token,
    )

    html_body, plain_text = render_manager_approval_email(
        manager_name="David",
        company_name=company["name"],
        cycle_label=batch["cycle_label"],
        batch_id=batch_id,
        submitted_by="Maria Hernandez",
        totals=batch["totals"],
        exception_count=exception_count,
        admin_notes=batch["admin_notes"],
        handoff_token=handoff_token,
        approve_token=approve_token,
        reject_token=reject_token,
        base_url=s.app_base_url,
        inline_card=inline_card,
    )

    return {
        "subject": f"✅ Approval needed · {batch_id} · ${batch['totals']['gross']:,.2f}",
        "html_body": html_body,
        "plain_text": plain_text,
        "inline_card": inline_card,
        "handoff_token": handoff_token,
        "batch_id": batch_id,
        "batch": batch,
        "company": company,
        "exception_count": exception_count,
    }


# ---- Channel dispatchers ----


def _parse_channels(delivery_mode: str) -> set[str]:
    mode = (delivery_mode or "email").lower().strip()
    if mode == "both":
        return {"email", "teams"}
    if mode in ("email", "teams"):
        return {mode}
    return {"email"}


async def _deliver_via_email(*, to: str, subject: str, html_body: str, plain_text: str) -> str:
    try:
        op_id = await send_email(to=to, subject=subject, html_body=html_body, plain_text=plain_text)
        return f"📧 Email queued to {to} (op={str(op_id)[-12:]})"
    except Exception as e:
        logger.exception("email send failed")
        return f"📧 ❌ Email failed: {type(e).__name__}: {e}"


async def _deliver_admin_via_teams(artifacts: dict) -> str:
    """Push the admin worklist card directly into the Teams bot chat (no email)."""
    s = get_settings()
    conv_store = get_conversation_store()
    persona = "payroll_admin"
    stored = None
    for em in (s.demo_user_email, s.demo_admin_email):
        if not em:
            continue
        stored = conv_store.get_by_user(em, persona)
        if stored and stored.conversation_reference.get("conversation", {}).get("id"):
            break
        stored = None
    if not stored:
        return ("💬 ❌ Teams: no conversation reference for the admin persona yet. "
                "Open the PayCycle bot in Teams and send one message ('hi'), then retry.")

    teams_card = build_exception_worklist_card(
        event_id=artifacts["handoff_token"][-12:],
        batch_id=artifacts["batch_id"],
        company_name=artifacts["company"]["name"],
        cycle_label=artifacts["cycle"]["label"],
        deadline_iso=artifacts["cycle"]["deadline"],
        exceptions=artifacts["exceptions"],
        totals={
            "employees": artifacts["cycle"].get("employees_included", 0),
            "gross": artifacts["cycle"].get("estimated_gross", 0),
            "net": artifacts["cycle"].get("estimated_net", 0),
        },
        persona=persona,
        user_email=stored.user_email,
    )

    try:
        await push_card_to_stored(
            stored, teams_card,
            text=(f"🔔 PayCycle: **{len(artifacts['exceptions'])} payroll exceptions** need review "
                  f"for cycle **{artifacts['cycle']['label']}**."),
        )
        return f"💬 Teams card pushed to {stored.user_email} ({stored.surface})"
    except Exception as e:
        logger.exception("proactive admin push failed")
        return f"💬 ❌ Teams push failed: {type(e).__name__}: {e}"


async def _deliver_manager_via_teams(artifacts: dict) -> str:
    """Push the manager approval context card directly into Teams (no email)."""
    s = get_settings()
    conv_store = get_conversation_store()
    persona = "payroll_manager"
    stored = None
    for em in (s.demo_user_email, s.demo_manager_email):
        if not em:
            continue
        stored = conv_store.get_by_user(em, persona)
        if stored and stored.conversation_reference.get("conversation", {}).get("id"):
            break
        stored = None
    if not stored:
        return ("💬 ❌ Teams: no conversation reference for the manager persona yet. "
                "Open the PayCycle bot in Teams and send one message ('hi'), then retry.")

    batch = artifacts["batch"]
    facts = [
        {"title": "Batch", "value": batch["id"]},
        {"title": "Cycle", "value": batch["cycle_label"]},
        {"title": "Status", "value": batch["status"].title()},
        {"title": "Employees", "value": f"{batch['totals']['employees']:,}"},
        {"title": "Gross", "value": f"${batch['totals']['gross']:,.2f}"},
        {"title": "Exceptions resolved", "value": str(artifacts["exception_count"])},
    ]
    teams_card = build_teams_continuation_card(
        title=f"Approval needed · {batch['cycle_label']}",
        summary=("Maria submitted this batch and is asking for your approval. I have the full "
                 "context — totals, exception resolutions, audit trail. Ask me anything or approve below."),
        facts=facts,
        base_url=s.app_base_url,
        primary_action_label="✅ Approve this batch",
        primary_action_data={
            "verb": "approve_batch",
            "batch_id": batch["id"],
            "approver": stored.user_email,
            "event_id": artifacts["handoff_token"][-12:],
        },
    )

    try:
        await push_card_to_stored(
            stored, teams_card,
            text=f"✅ PayCycle: batch **{batch['id']}** is submitted and needs your approval.",
        )
        return f"💬 Teams card pushed to {stored.user_email} ({stored.surface})"
    except Exception as e:
        logger.exception("proactive manager push failed")
        return f"💬 ❌ Teams push failed: {type(e).__name__}: {e}"


# ---- POST handlers ----


@router.post("/send-admin-alert")
async def send_admin_alert(delivery_mode: str = Form("email")) -> RedirectResponse:
    s = get_settings()
    artifacts = _build_admin_artifacts()
    if artifacts.get("empty"):
        return RedirectResponse("/demo/console?flash=No+open+exceptions+to+send.", status_code=303)

    channels = _parse_channels(delivery_mode)
    results: list[str] = []
    tasks = []
    if "email" in channels:
        tasks.append(_deliver_via_email(
            to=s.demo_user_email,
            subject=artifacts["subject"],
            html_body=artifacts["html_body"],
            plain_text=artifacts["plain_text"],
        ))
    if "teams" in channels:
        tasks.append(_deliver_admin_via_teams(artifacts))
    for r in await asyncio.gather(*tasks, return_exceptions=False):
        results.append(r)

    flash = "Admin notification → " + "  |  ".join(results)
    return RedirectResponse(f"/demo/console?flash={flash}", status_code=303)


@router.post("/submit-batch")
async def submit_batch(
    batch_id: str = Form("BATCH-2026-05B"),
    delivery_mode: str = Form("email"),
) -> RedirectResponse:
    s = get_settings()
    try:
        artifacts = _build_manager_artifacts(batch_id)
    except (KeyError, ValueError) as e:
        return RedirectResponse(f"/demo/console?flash=Submit+failed:+{e}", status_code=303)

    channels = _parse_channels(delivery_mode)
    results: list[str] = []
    tasks = []
    if "email" in channels:
        tasks.append(_deliver_via_email(
            to=s.demo_user_email,
            subject=artifacts["subject"],
            html_body=artifacts["html_body"],
            plain_text=artifacts["plain_text"],
        ))
    if "teams" in channels:
        tasks.append(_deliver_manager_via_teams(artifacts))
    for r in await asyncio.gather(*tasks, return_exceptions=False):
        results.append(r)

    flash = "Manager approval → " + "  |  ".join(results)
    return RedirectResponse(f"/demo/console?flash={flash}", status_code=303)


@router.post("/reset")
async def reset() -> RedirectResponse:
    get_store().reset()
    return RedirectResponse("/demo/console?flash=Demo+state+reset.", status_code=303)


@router.get("/state")
async def state() -> JSONResponse:
    """Programmatic state dump - useful when scripting demos."""
    store = get_store()
    return JSONResponse({
        "company": store.get_company(),
        "cycle": store.get_current_cycle(),
        "open_exceptions": store.list_open_exceptions(),
        "batches": {bid: store.get_batch(bid) for bid in ["BATCH-2026-05B"]},
        "audit": store.get_audit_log(),
    })

"""Demo console - lightweight web UI for triggering scenarios during a live demo."""
from __future__ import annotations
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..cards.builders import (
    build_admin_exception_notification,
    build_manager_approval_request,
)
from ..common.config import get_settings
from ..common.logging import get_logger
from ..common.tokens import mint
from ..email_service.sender import send_email
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
    .flash {{ background: #dff6dd; color: #107c10; padding: 10px 16px; border-radius: 6px; margin: 8px 0; }}
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
    <h3>Step 1 - Send exception alert email to Payroll Admin</h3>
    <p>Sends an Outlook actionable email to <b>{demo_email}</b> playing the <span class="pill">payroll_admin</span> persona (Maria).
    The email contains the open exceptions and a button to continue review in Teams.</p>
    <form method="post" action="/demo/send-admin-alert">
      <button type="submit">Send admin alert email</button>
    </form>
  </div>

  <div class="step">
    <h3>Step 2 - Admin submits batch for approval</h3>
    <p>Simulates Maria resolving the exceptions and submitting the batch to the Payroll Manager (David).
    This sends a second actionable email containing the approval request card.</p>
    <form method="post" action="/demo/submit-batch">
      <input type="hidden" name="batch_id" value="BATCH-2026-05B">
      <button type="submit">Submit batch & email approver</button>
    </form>
  </div>

  <div class="step">
    <h3>Step 3 - (in your inbox) Approve directly OR click 'Get details in Teams'</h3>
    <p>You now play David. In Outlook, you can either approve in-email (happy path) or click
    'Get details in Teams' to be handed off into a Teams chat with the agent that has full
    context loaded.</p>
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

    exception_rows = []
    for e in store.list_open_exceptions():
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
        batch_rows="".join(batch_rows) or "<tr><td colspan='4' class='meta'>No batches.</td></tr>",
        exception_rows="".join(exception_rows),
        audit_rows="".join(audit_rows),
        flash=f"<div class='flash'>{flash}</div>" if flash else "",
    )


@router.get("/console", response_class=HTMLResponse)
async def console(request: Request) -> HTMLResponse:
    flash = request.query_params.get("flash", "")
    return HTMLResponse(_render(flash))


@router.post("/send-admin-alert")
async def send_admin_alert() -> RedirectResponse:
    s = get_settings()
    store = get_store()
    company = store.get_company()
    cycle = store.get_current_cycle()
    exceptions = store.list_open_exceptions()

    if not exceptions:
        return RedirectResponse("/demo/console?flash=No+open+exceptions+to+send.", status_code=303)

    handoff_token = mint(
        purpose="handoff",
        subject=s.demo_user_email,
        extra={
            "persona": "payroll_admin",
            "batch_id": "BATCH-2026-05B",
            "intent": "review_exceptions",
            "event": f"alert/{cycle['id']}",
        },
        ttl_seconds=86400,
    )

    card = build_admin_exception_notification(
        company_name=company["name"],
        cycle_label=cycle["label"],
        deadline_iso=cycle["deadline"],
        exceptions=exceptions,
        base_url=s.app_base_url,
        discuss_token=handoff_token,
    )

    try:
        await send_email(
            to=s.demo_user_email,
            subject=f"[PayCycle] {len(exceptions)} exceptions need review · {cycle['label']}",
            card=card,
            fallback_text=f"{len(exceptions)} payroll exceptions need your attention for {cycle['label']}.",
            fallback_link=f"{s.app_base_url}/cta/handoff?token={handoff_token}&surface=teams",
        )
        flash = f"Admin alert email queued to {s.demo_user_email}. Check the inbox in ~30 seconds."
    except Exception as e:
        flash = f"Email send failed: {e}"
        logger.exception("admin alert email failed")
    return RedirectResponse(f"/demo/console?flash={flash}", status_code=303)


@router.post("/submit-batch")
async def submit_batch(batch_id: str = Form("BATCH-2026-05B")) -> RedirectResponse:
    s = get_settings()
    store = get_store()

    # Simulate Maria having resolved the exceptions
    for exc in store.list_open_exceptions():
        store.resolve_exception(
            exc["id"],
            resolver="Maria Hernandez (Payroll Admin)",
            notes=f"Reviewed and approved by admin on {datetime.utcnow().isoformat()}Z",
        )

    try:
        batch = store.submit_batch(
            batch_id,
            submitted_by="Maria Hernandez (Payroll Admin)",
            admin_notes=(
                "Joseph Smith's 14h overtime is legitimate - confirmed assignment to Project Atlas tooling rebuild. "
                "Sarah Lee's PTO of 6h approved verbally by Daniel Cruz this morning; PTO-9821 will be updated in Flex by EOD."
            ),
        )
    except (KeyError, ValueError) as e:
        return RedirectResponse(f"/demo/console?flash=Submit+failed:+{e}", status_code=303)

    company = store.get_company()
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

    card = build_manager_approval_request(
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

    try:
        await send_email(
            to=s.demo_user_email,
            subject=f"[PayCycle] Approval needed · {batch_id} · ${batch['totals']['gross']:,.2f}",
            card=card,
            fallback_text=f"Payroll batch {batch_id} is ready for your approval.",
            fallback_link=f"{s.app_base_url}/cta/handoff?token={handoff_token}&surface=teams",
        )
        flash = f"Approval email queued to {s.demo_user_email}. Click Approve or 'Get details in Teams' when it arrives."
    except Exception as e:
        flash = f"Email send failed: {e}"
        logger.exception("approval email failed")
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

"""Plain-HTML email templates - Defender-safe, no actionable card script tag.

These render directly in any modern mail client (Outlook web/desktop/mobile, Gmail,
Apple Mail). They contain enough information for the user to triage from their
inbox, and a primary CTA that deep-links into Teams (or a secondary web fallback).

Optional OAM-enhancement: pass `card=<adaptive card dict>` to embed it as a
`<script type="application/adaptivecard+json">` block at the top. Outlook clients
that recognize the OAM originator will render the card inline above the HTML.
Everyone else (and anyone whose tenant strips the script via Defender) sees the
rich HTML body.
"""
from __future__ import annotations
import json
from typing import Optional

from ..common.config import get_settings


def _money(amount: float) -> str:
    return f"${amount:,.2f}"


def _inline_card_script(card: Optional[dict]) -> str:
    """Render the OAM Adaptive Card as a <script> tag for Outlook-OAM clients.

    The card SHOULD already include 'originator' (set by `_wrap_for_outlook`).
    For Outlook clients that recognize this originator, the card renders inline
    above the HTML below. For everyone else (and tenants whose Defender strips
    the script), the rich HTML body is the only thing they see.
    """
    if not card:
        return ""
    return f'<script type="application/adaptivecard+json">{json.dumps(card)}</script>'


def _btn(label: str, url: str, color: str = "#2667ff", text_color: str = "#ffffff") -> str:
    return (
        f'<a href="{url}" '
        f'style="display:inline-block;background:{color};color:{text_color};padding:11px 22px;'
        f'border-radius:6px;text-decoration:none;font-family:Segoe UI,Arial,sans-serif;'
        f'font-size:14px;font-weight:600;margin:0 8px 8px 0">{label}</a>'
    )


def _sev_badge(severity: str) -> str:
    palette = {
        "warning": ("#fff3cd", "#664d03", "WARNING"),
        "error": ("#f8d7da", "#842029", "ERROR"),
        "info": ("#cfe2ff", "#084298", "INFO"),
    }
    bg, fg, label = palette.get(severity, ("#e9ecef", "#495057", severity.upper()))
    return (
        f'<span style="display:inline-block;background:{bg};color:{fg};padding:2px 8px;'
        f'border-radius:10px;font-size:11px;font-weight:600;letter-spacing:.3px">{label}</span>'
    )


def render_admin_exception_email(
    *,
    admin_name: str,
    company_name: str,
    cycle_label: str,
    deadline_iso: str,
    exceptions: list[dict],
    handoff_token: str,
    base_url: str,
    web_url: str = "https://example.invalid/flex/payroll/batches",
    total_employees: int = 0,
    total_gross: float = 0.0,
    inline_card: Optional[dict] = None,
) -> tuple[str, str]:
    """Render the admin-alert email. Returns (html_body, plain_text_body).

    If `inline_card` is provided, it's embedded as a
    `<script type="application/adaptivecard+json">` block in <head>. Outlook clients
    with the OAM originator registered will render it inline; others get the HTML below.
    """
    s = get_settings()

    deadline_display = deadline_iso.replace("T", " ").split("+")[0] + " local"
    n = len(exceptions)
    plural = "s" if n != 1 else ""

    rows = ""
    for exc in exceptions:
        rows += f"""
        <tr>
          <td style="padding:14px 12px;border-bottom:1px solid #f0f0f0;vertical-align:top">
            <div style="font-weight:600;color:#222">{exc['employee_name']}</div>
            <div style="font-size:12px;color:#888;margin-top:2px">{exc['id']}</div>
          </td>
          <td style="padding:14px 12px;border-bottom:1px solid #f0f0f0;vertical-align:top">
            <div style="color:#222">{exc['title']}</div>
            <div style="font-size:13px;color:#555;margin-top:4px;line-height:1.45">{exc['summary']}</div>
            <div style="margin-top:6px">{_sev_badge(exc['severity'])}</div>
          </td>
          <td style="padding:14px 12px;border-bottom:1px solid #f0f0f0;vertical-align:top;text-align:right;white-space:nowrap;font-weight:600;color:#222">
            {_money(exc['amount_impact'])}
          </td>
        </tr>
        """

    teams_url = f"{base_url}/cta/handoff?token={handoff_token}&surface=teams"
    web_btn = _btn("Open in PayCycle (web)", web_url, color="#ffffff", text_color="#2667ff") if web_url else ""
    web_btn = web_btn.replace('color:#2667ff', 'color:#2667ff;border:1px solid #2667ff') if web_btn else ""

    cycle_meta = ""
    if total_employees or total_gross:
        cycle_meta = (
            f'<div style="font-size:13px;color:#666;margin-top:8px">'
            f'{total_employees:,} employees · {_money(total_gross)} gross before exception resolution'
            f'</div>'
        )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
{_inline_card_script(inline_card)}
</head>
<body style="margin:0;padding:0;background:#f5f6fa;font-family:Segoe UI,Arial,sans-serif;color:#222">
  <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="background:#f5f6fa;padding:24px 0">
    <tr><td align="center">
      <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="640" style="max-width:640px;background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.08)">

        <tr><td style="padding:24px 28px 0 28px">
          <div style="color:#2667ff;font-size:12px;font-weight:600;letter-spacing:.6px;text-transform:uppercase">PayCycle Alert</div>
          <h1 style="margin:6px 0 0 0;font-size:22px;color:#1a1a1a;font-weight:600">
            🔔 {n} payroll exception{plural} need{'' if n != 1 else 's'} your review
          </h1>
          <div style="color:#666;font-size:14px;margin-top:6px">{company_name} · {cycle_label}</div>
        </td></tr>

        <tr><td style="padding:18px 28px 0 28px">
          <p style="margin:0;font-size:14px;line-height:1.5;color:#333">
            Hi {admin_name}, your current pay cycle has {n} exception{plural} blocking submission.
            Review the details below and resolve them in Teams or PayCycle web.
          </p>
        </td></tr>

        <tr><td style="padding:18px 28px 0 28px">
          <table cellspacing="0" cellpadding="0" border="0" width="100%" style="border:1px solid #ececec;border-radius:6px;overflow:hidden">
            <thead>
              <tr style="background:#fafafa">
                <th style="text-align:left;padding:10px 12px;font-size:12px;color:#666;font-weight:600;letter-spacing:.3px;text-transform:uppercase;border-bottom:1px solid #ececec">Employee</th>
                <th style="text-align:left;padding:10px 12px;font-size:12px;color:#666;font-weight:600;letter-spacing:.3px;text-transform:uppercase;border-bottom:1px solid #ececec">Issue</th>
                <th style="text-align:right;padding:10px 12px;font-size:12px;color:#666;font-weight:600;letter-spacing:.3px;text-transform:uppercase;border-bottom:1px solid #ececec">$ Impact</th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
          </table>
        </td></tr>

        <tr><td style="padding:18px 28px 0 28px">
          <div style="background:#fff3cd;color:#664d03;padding:10px 14px;border-radius:6px;font-size:13px;border-left:3px solid #ffc107">
            ⏰  <strong>Cycle deadline:</strong> {deadline_display}
            {cycle_meta}
          </div>
        </td></tr>

        <tr><td style="padding:22px 28px 28px 28px">
          {_btn("💬 Review in Teams", teams_url, color="#2667ff")}
          {web_btn}
        </td></tr>

        <tr><td style="padding:0 28px 24px 28px">
          <hr style="border:none;border-top:1px solid #ececec;margin:0 0 12px 0">
          <div style="font-size:11px;color:#999;line-height:1.5">
            Automated message from PayCycle Payroll Assistant. The "Review in Teams" button opens
            a chat in Microsoft Teams where the PayCycle agent has loaded all batch context.
          </div>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body></html>"""

    text_lines = [
        f"PayCycle Alert — {n} payroll exception{plural} need review",
        f"{company_name} · {cycle_label}",
        f"Deadline: {deadline_display}",
        "",
    ]
    for exc in exceptions:
        text_lines.append(f"• {exc['employee_name']} ({exc['id']}) · {exc['title']}")
        text_lines.append(f"  {exc['summary']}")
        text_lines.append(f"  Impact: {_money(exc['amount_impact'])} · Severity: {exc['severity'].upper()}")
        text_lines.append("")
    text_lines.append(f"Review in Teams: {teams_url}")
    if web_url:
        text_lines.append(f"Open in PayCycle web: {web_url}")
    plain = "\n".join(text_lines)

    return html, plain


def render_manager_approval_email(
    *,
    manager_name: str,
    company_name: str,
    cycle_label: str,
    batch_id: str,
    submitted_by: str,
    totals: dict,
    exception_count: int,
    admin_notes: str,
    handoff_token: str,
    approve_token: str,
    reject_token: str,
    base_url: str,
    inline_card: Optional[dict] = None,
) -> tuple[str, str]:
    """Render the manager-approval email. Returns (html_body, plain_text_body).

    If `inline_card` is provided (typically the OAM Approve/Reject card), it's embedded
    as an Adaptive Card script tag for Outlook OAM-capable clients.
    """
    teams_url = f"{base_url}/cta/handoff?token={handoff_token}&surface=teams"
    approve_url = f"{base_url}/cta/approve?token={approve_token}"

    notes_block = ""
    if admin_notes:
        notes_block = f"""
        <tr><td style="padding:0 28px 0 28px">
          <div style="background:#f6f8fa;padding:12px 14px;border-radius:6px;font-size:13px;color:#444;border-left:3px solid #2667ff">
            <div style="font-weight:600;margin-bottom:4px;color:#222">Notes from {submitted_by}</div>
            {admin_notes}
          </div>
        </td></tr>"""

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
{_inline_card_script(inline_card)}
</head>
<body style="margin:0;padding:0;background:#f5f6fa;font-family:Segoe UI,Arial,sans-serif;color:#222">
  <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" style="background:#f5f6fa;padding:24px 0">
    <tr><td align="center">
      <table role="presentation" cellspacing="0" cellpadding="0" border="0" width="640" style="max-width:640px;background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.08)">

        <tr><td style="padding:24px 28px 0 28px">
          <div style="color:#198754;font-size:12px;font-weight:600;letter-spacing:.6px;text-transform:uppercase">PayCycle Approval</div>
          <h1 style="margin:6px 0 0 0;font-size:22px;color:#1a1a1a;font-weight:600">
            ✅ Payroll batch ready for your approval
          </h1>
          <div style="color:#666;font-size:14px;margin-top:6px">{company_name} · {cycle_label}</div>
        </td></tr>

        <tr><td style="padding:18px 28px 0 28px">
          <p style="margin:0;font-size:14px;line-height:1.5;color:#333">
            Hi {manager_name}, {submitted_by} has submitted batch <strong>{batch_id}</strong> for your approval.
            {exception_count} exception{'s' if exception_count != 1 else ''} were resolved before submission.
          </p>
        </td></tr>

        <tr><td style="padding:18px 28px 0 28px">
          <table cellspacing="0" cellpadding="0" border="0" width="100%" style="border:1px solid #ececec;border-radius:6px">
            <tr><td style="padding:12px 14px;border-bottom:1px solid #f0f0f0">
              <span style="color:#666;font-size:13px">Employees</span>
              <span style="float:right;font-weight:600">{totals['employees']:,}</span>
            </td></tr>
            <tr><td style="padding:12px 14px;border-bottom:1px solid #f0f0f0">
              <span style="color:#666;font-size:13px">Gross payroll</span>
              <span style="float:right;font-weight:600">{_money(totals['gross'])}</span>
            </td></tr>
            <tr><td style="padding:12px 14px">
              <span style="color:#666;font-size:13px">Net to employees</span>
              <span style="float:right;font-weight:600">{_money(totals['net'])}</span>
            </td></tr>
          </table>
        </td></tr>
        {notes_block}

        <tr><td style="padding:22px 28px 28px 28px">
          {_btn("💬 Review in Teams", teams_url, color="#2667ff")}
        </td></tr>

        <tr><td style="padding:0 28px 24px 28px">
          <hr style="border:none;border-top:1px solid #ececec;margin:0 0 12px 0">
          <div style="font-size:11px;color:#999;line-height:1.5">
            Automated message from PayCycle. Approve in Teams to see batch details and audit trail.
          </div>
        </td></tr>

      </table>
    </td></tr>
  </table>
</body></html>"""

    plain = (
        f"PayCycle Approval — Batch {batch_id}\n"
        f"{company_name} · {cycle_label}\n"
        f"Submitted by: {submitted_by}\n"
        f"Employees: {totals['employees']:,} · Gross: {_money(totals['gross'])} · Net: {_money(totals['net'])}\n"
        f"Exceptions resolved: {exception_count}\n"
        + (f"Notes: {admin_notes}\n" if admin_notes else "")
        + f"\nReview in Teams: {teams_url}\n"
    )
    return html, plain

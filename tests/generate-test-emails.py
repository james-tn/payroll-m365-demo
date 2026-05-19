#!/usr/bin/env python3
"""Generate VBA test-sender macros for both delivery modes.

Two .bas files are produced (next to this script):

  SendOamCardTest.bas   - Mode A: Outlook Actionable Card + rich HTML fallback
                          For tenants that have the full OAM provider wired up.
                          Renders the inline card with per-row Approve / Flag
                          buttons. Non-OAM clients see the HTML body underneath.

  SendOamHtmlTest.bas   - Mode B: Plain styled HTML email + 'Review' CTA only
                          Zero customer-side configuration needed. Works in
                          Outlook (desktop/web/mobile), Gmail, Apple Mail, etc.
                          Click 'Review with Assistant' deep-links to Teams.

Run from the repo root:

    uv run python tests/generate-test-emails.py \\
        --originator <OAM-PROVIDER-GUID> \\
        --approver  james.nguyen@microsoft.com \\
        --recipient james.nguyen@microsoft.com

Then in Outlook desktop: Alt+F11 -> Remove old SendOam* modules ->
File > Import File... -> import BOTH .bas files -> F5 in whichever you want.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.common.tokens import mint  # noqa: E402

BASE_URL = "https://payroll-m365-demo.politeground-c0ea36c5.eastus2.azurecontainerapps.io"
BATCH_ID = "BATCH-2026-05B"

EXCEPTIONS = [
    {
        "id": "EXC-2026-05B-001",
        "employee_name": "Joseph Smith",
        "title": "Overtime variance high",
        "summary": "14.5h OT vs 4h avg (261% over 6 periods).",
        "amount_impact": 847.50,
        "severity": "warning",
        "style": "warning",
    },
    {
        "id": "EXC-2026-05B-002",
        "employee_name": "Sarah Lee",
        "title": "PTO missing manager approval",
        "summary": "3 days PTO 2026-05-25..27, no approver action.",
        "amount_impact": 1240.00,
        "severity": "info",
        "style": "accent",
    },
]


def handoff_url(approver: str) -> str:
    token = mint(
        purpose="handoff",
        subject=approver,
        extra={
            "persona": "payroll_admin",
            "batch_id": BATCH_ID,
            "intent": "review_exceptions",
            "event": "alert/CYC-2026-05B",
            "exception_ids": [e["id"] for e in EXCEPTIONS],
        },
        ttl_seconds=86400,
    )
    return f"{BASE_URL}/cta/handoff?token={token}&surface=teams"


def card_dict(originator: str, approver: str) -> dict:
    body: list[dict] = [
        {"type": "TextBlock", "text": "Payroll exceptions need your review",
         "size": "Large", "weight": "Bolder", "wrap": True},
        {"type": "TextBlock", "text": "Acme Manufacturing - Pay period ending May 30, 2026",
         "isSubtle": True, "wrap": True, "spacing": "None"},
        {"type": "FactSet", "spacing": "Medium", "facts": [
            {"title": "Cycle", "value": "Pay period ending May 30, 2026"},
            {"title": "Deadline", "value": "2026-05-30 17:00 PT"},
            {"title": "Open exceptions", "value": str(len(EXCEPTIONS))},
            {"title": "Estimated impact", "value": "$2,087.50"},
        ]},
    ]
    for exc in EXCEPTIONS:
        body.append({
            "type": "Container",
            "style": exc["style"],
            "spacing": "Medium",
            "items": [
                {"type": "TextBlock", "text": f"{exc['employee_name']} - {exc['title']}",
                 "weight": "Bolder", "wrap": True},
                {"type": "TextBlock", "text": exc["summary"], "wrap": True, "spacing": "Small"},
                {"type": "ActionSet", "spacing": "Small", "actions": [
                    {"type": "Action.Http", "title": "✅ Approve",
                     "method": "POST",
                     "url": f"{BASE_URL}/cta/oam/approve-exception/{exc['id']}",
                     "body": "",
                     "headers": [{"name": "Content-Type", "value": "application/json"}]},
                    {"type": "Action.Http", "title": "🚩 Flag for HR",
                     "method": "POST",
                     "url": f"{BASE_URL}/cta/oam/flag-exception/{exc['id']}",
                     "body": "",
                     "headers": [{"name": "Content-Type", "value": "application/json"}]},
                ]},
            ],
        })
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "originator": originator,
        "hideOriginalBody": False,
        "body": body,
        "actions": [
            {"type": "Action.OpenUrl",
             "title": "Review with PayCycle Assistant",
             "url": handoff_url(approver)},
        ],
    }


def html_body(approver: str) -> str:
    """Plain HTML body shown below the card (or by itself in html-only mode)."""
    rows = ""
    for exc in EXCEPTIONS:
        sev_bg, sev_fg = {
            "warning": ("#fff3cd", "#664d03"),
            "error":   ("#f8d7da", "#842029"),
            "info":    ("#cfe2ff", "#084298"),
        }.get(exc["severity"], ("#e9ecef", "#495057"))
        rows += (
            f'<tr>'
            f'<td style="padding:14px 12px;border-bottom:1px solid #f0f0f0;vertical-align:top">'
            f'  <div style="font-weight:600;color:#222">{exc["employee_name"]}</div>'
            f'  <div style="font-size:12px;color:#888;margin-top:2px">{exc["id"]}</div>'
            f'</td>'
            f'<td style="padding:14px 12px;border-bottom:1px solid #f0f0f0;vertical-align:top">'
            f'  <div style="color:#222">{exc["title"]}</div>'
            f'  <div style="font-size:13px;color:#555;margin-top:4px;line-height:1.45">{exc["summary"]}</div>'
            f'  <div style="margin-top:6px">'
            f'    <span style="display:inline-block;background:{sev_bg};color:{sev_fg};padding:2px 8px;'
            f'border-radius:10px;font-size:11px;font-weight:600;letter-spacing:.3px">'
            f'{exc["severity"].upper()}</span></div>'
            f'</td>'
            f'<td style="padding:14px 12px;border-bottom:1px solid #f0f0f0;vertical-align:top;'
            f'text-align:right;white-space:nowrap;font-weight:600;color:#222">'
            f'${exc["amount_impact"]:,.2f}</td>'
            f'</tr>'
        )
    cta = handoff_url(approver)
    return (
        '<body style="margin:0;padding:0;background:#f5f6fa;font-family:Segoe UI,Arial,sans-serif;color:#222">'
        '<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="100%" '
        'style="background:#f5f6fa;padding:24px 0">'
        '<tr><td align="center">'
        '<table role="presentation" cellspacing="0" cellpadding="0" border="0" width="640" '
        'style="max-width:640px;background:#ffffff;border-radius:8px;overflow:hidden;'
        'box-shadow:0 1px 3px rgba(0,0,0,.08)">'
        '<tr><td style="padding:24px 28px 0 28px">'
        '<div style="color:#2667ff;font-size:12px;font-weight:600;letter-spacing:.6px;text-transform:uppercase">'
        'PayCycle Alert</div>'
        '<h1 style="margin:6px 0 0 0;font-size:22px;color:#1a1a1a;font-weight:600">'
        '🔔 2 payroll exceptions need your review</h1>'
        '<div style="color:#666;font-size:14px;margin-top:6px">Acme Manufacturing · Pay period ending May 30, 2026</div>'
        '</td></tr>'
        '<tr><td style="padding:18px 28px 0 28px">'
        '<p style="margin:0;font-size:14px;line-height:1.5;color:#333">'
        'Your current pay cycle has 2 exceptions blocking submission. Review the details below and '
        'resolve them in Teams or the web app.</p>'
        '</td></tr>'
        '<tr><td style="padding:18px 28px 0 28px">'
        '<table cellspacing="0" cellpadding="0" border="0" width="100%" '
        'style="border:1px solid #ececec;border-radius:6px;overflow:hidden">'
        '<thead><tr style="background:#fafafa">'
        '<th style="text-align:left;padding:10px 12px;font-size:12px;color:#666;font-weight:600;'
        'letter-spacing:.3px;text-transform:uppercase;border-bottom:1px solid #ececec">Employee</th>'
        '<th style="text-align:left;padding:10px 12px;font-size:12px;color:#666;font-weight:600;'
        'letter-spacing:.3px;text-transform:uppercase;border-bottom:1px solid #ececec">Issue</th>'
        '<th style="text-align:right;padding:10px 12px;font-size:12px;color:#666;font-weight:600;'
        'letter-spacing:.3px;text-transform:uppercase;border-bottom:1px solid #ececec">$ Impact</th>'
        '</tr></thead>'
        f'<tbody>{rows}</tbody>'
        '</table>'
        '</td></tr>'
        '<tr><td style="padding:18px 28px 0 28px">'
        '<div style="background:#fff3cd;color:#664d03;padding:10px 14px;border-radius:6px;'
        'font-size:13px;border-left:3px solid #ffc107">'
        '⏰ <strong>Cycle deadline:</strong> 2026-05-30 17:00 PT</div>'
        '</td></tr>'
        '<tr><td style="padding:22px 28px 28px 28px">'
        f'<a href="{cta}" style="display:inline-block;background:#2667ff;color:#ffffff;'
        'padding:11px 22px;border-radius:6px;text-decoration:none;font-family:Segoe UI,Arial,sans-serif;'
        'font-size:14px;font-weight:600">💬 Review with PayCycle Assistant</a>'
        '</td></tr>'
        '<tr><td style="padding:0 28px 24px 28px">'
        '<hr style="border:none;border-top:1px solid #ececec;margin:0 0 12px 0">'
        '<div style="font-size:11px;color:#999;line-height:1.5">'
        'Automated message from PayCycle. The CTA opens a Teams chat where the assistant has '
        'loaded all batch context for this email.</div>'
        '</td></tr>'
        '</table></td></tr></table>'
        '</body>'
    )


def vba_escape(s: str) -> str:
    return s.replace('"', '""')


def chunks(s: str, n: int):
    for i in range(0, len(s), n):
        yield s[i:i + n]


def chunked_concat(varname: str, payload: str, indent: str = "    ") -> str:
    """Emit `var = ""` + `var = var & "chunk"` lines under VBA's continuation cap."""
    out = [f'{indent}{varname} = ""']
    for chunk in chunks(payload, 180):
        out.append(f'{indent}{varname} = {varname} & "{vba_escape(chunk)}"')
    return "\n".join(out)


def build_card_bas(card_json: str, html: str, recipient: str) -> str:
    return f"""Attribute VB_Name = "SendOamCardTest"
' Auto-generated by tests/generate-test-emails.py
' MODE A: Adaptive Card (script tag in <head>) + rich HTML body fallback.
' For tenants with the full Outlook Actionable Messages provider configured.
'
' Import: Outlook -> Alt+F11 -> File > Import File... > SendOamCardTest.bas
' Run:    place cursor in SendOamCardTest, press F5.

Option Explicit

Public Sub SendOamCardTest()
    Const RECIPIENT As String = "{vba_escape(recipient)}"
    Const SUBJ      As String = "[Test] Actionable Card mode - payroll exceptions"

    Dim cj As String
{chunked_concat("cj", card_json)}

    Dim hb As String
{chunked_concat("hb", html)}

    Dim html As String
    html = "<!DOCTYPE html><html><head><meta charset=""utf-8"">" & _
           "<script type=""application/adaptivecard+json"">" & cj & "</script>" & _
           "</head>" & hb & "</html>"

    Dim msg As Outlook.MailItem
    Set msg = Application.CreateItem(olMailItem)
    msg.To = RECIPIENT
    msg.Subject = SUBJ
    msg.HTMLBody = html
    msg.Send

    MsgBox "Sent CARD-mode test to " & RECIPIENT, vbInformation, "Test sender"
End Sub
"""


def build_html_bas(html: str, recipient: str) -> str:
    return f"""Attribute VB_Name = "SendOamHtmlTest"
' Auto-generated by tests/generate-test-emails.py
' MODE B: Plain styled HTML email + 'Review' CTA only. Zero customer-side
' configuration required - works in any modern mail client.
'
' Import: Outlook -> Alt+F11 -> File > Import File... > SendOamHtmlTest.bas
' Run:    place cursor in SendOamHtmlTest, press F5.

Option Explicit

Public Sub SendOamHtmlTest()
    Const RECIPIENT As String = "{vba_escape(recipient)}"
    Const SUBJ      As String = "[Test] HTML-only mode - payroll exceptions"

    Dim hb As String
{chunked_concat("hb", html)}

    Dim html As String
    html = "<!DOCTYPE html><html><head><meta charset=""utf-8""></head>" & hb & "</html>"

    Dim msg As Outlook.MailItem
    Set msg = Application.CreateItem(olMailItem)
    msg.To = RECIPIENT
    msg.Subject = SUBJ
    msg.HTMLBody = html
    msg.Send

    MsgBox "Sent HTML-only test to " & RECIPIENT, vbInformation, "Test sender"
End Sub
"""


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--originator", required=True, help="OAM originator GUID (only used by card mode)")
    p.add_argument("--approver", required=True, help="email used as 'sub' in signed handoff/action tokens")
    p.add_argument("--recipient", required=True, help="email recipient (where the test mail is sent)")
    p.add_argument("--outdir", default=str(Path(__file__).resolve().parent))
    p.add_argument("--mode", default="both", choices=["both", "card", "html"])
    args = p.parse_args()

    outdir = Path(args.outdir)
    html = html_body(args.approver)

    written = []
    if args.mode in ("both", "card"):
        card = card_dict(args.originator, args.approver)
        card_json = json.dumps(card, separators=(",", ":"))
        card_path = outdir / "SendOamCardTest.bas"
        card_path.write_text(build_card_bas(card_json, html, args.recipient), encoding="utf-8")
        written.append((card_path, f"card({len(card_json)} chars) + html({len(html)} chars)"))

    if args.mode in ("both", "html"):
        html_path = outdir / "SendOamHtmlTest.bas"
        html_path.write_text(build_html_bas(html, args.recipient), encoding="utf-8")
        written.append((html_path, f"html({len(html)} chars)"))

    for path, meta in written:
        print(f"Wrote {path}  ({path.stat().st_size:,} bytes)  payload: {meta}")
    print()
    print("Next steps in Outlook desktop:")
    print("  1. Alt+F11 to open the VBA editor")
    print("  2. Remove any previous SendOam*Test modules (right-click -> Remove)")
    print("  3. File -> Import File... -> import each .bas above")
    print("  4. Place cursor in the macro you want to test, press F5")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

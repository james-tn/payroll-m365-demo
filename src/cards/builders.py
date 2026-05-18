"""Adaptive Card builders for the demo's three notification types.

All cards use Adaptive Card schema v1.5 (works in Outlook, Teams, Copilot Chat).
Action buttons use:
  - Action.Http for Outlook actionable buttons (resolves in email)
  - Action.OpenUrl for the Teams/Copilot handoff button (uses /cta/handoff redirect)
  - Action.Execute for Teams/Copilot inline buttons (round-trips to bot via invoke)
"""
from __future__ import annotations
from typing import Any, Optional

from ..common.config import get_settings


# ---- Helpers ----

def _money(amount: float) -> str:
    return f"${amount:,.2f}"


def _sev_color(severity: str) -> str:
    return {"warning": "warning", "error": "attention", "info": "accent"}.get(severity, "default")


def _wrap_for_outlook(card: dict) -> dict:
    """Outlook actionable messages require originator + hostConfig; Teams/Copilot tolerate them."""
    settings = get_settings()
    out = dict(card)
    if settings.oam_originator_id:
        out["originator"] = settings.oam_originator_id
    return out


# ---- Card: payroll admin gets notified of cycle exceptions ----

def build_admin_exception_notification(
    company_name: str,
    cycle_label: str,
    deadline_iso: str,
    exceptions: list[dict],
    base_url: str,
    discuss_token: str,
    open_flex_url: str = "https://example.invalid/flex/payroll/batches",
) -> dict:
    """Email sent to Payroll Admin when exceptions are detected.

    Two action paths:
      [Review in Copilot/Teams] -> Action.OpenUrl to /cta/handoff (push card + redirect to Teams)
      [Open in PayCycle] -> Action.OpenUrl to the Flex web app (mock - external link)
    """
    facts = [
        {"title": "Cycle", "value": cycle_label},
        {"title": "Company", "value": company_name},
        {"title": "Deadline", "value": deadline_iso.replace("T", " ").split("+")[0] + " local"},
        {"title": "Open exceptions", "value": str(len(exceptions))},
    ]

    exception_items: list[dict] = []
    for exc in exceptions:
        exception_items.extend([
            {
                "type": "Container",
                "style": _sev_color(exc["severity"]),
                "spacing": "Medium",
                "items": [
                    {
                        "type": "ColumnSet",
                        "columns": [
                            {
                                "type": "Column",
                                "width": "stretch",
                                "items": [
                                    {"type": "TextBlock", "text": exc["title"], "weight": "Bolder", "wrap": True},
                                    {"type": "TextBlock", "text": f"{exc['employee_name']} · {exc['id']}", "isSubtle": True, "spacing": "None", "wrap": True},
                                    {"type": "TextBlock", "text": exc["summary"], "wrap": True, "spacing": "Small"},
                                ],
                            },
                            {
                                "type": "Column",
                                "width": "auto",
                                "items": [
                                    {"type": "TextBlock", "text": _money(exc["amount_impact"]), "weight": "Bolder", "horizontalAlignment": "Right"},
                                    {"type": "TextBlock", "text": exc["severity"].upper(), "size": "Small", "color": _sev_color(exc["severity"]), "horizontalAlignment": "Right", "spacing": "None"},
                                ],
                            },
                        ],
                    }
                ],
            }
        ])

    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": [
            {
                "type": "Container",
                "items": [
                    {"type": "TextBlock", "text": f"⚠️  Payroll exceptions need your review", "size": "Large", "weight": "Bolder", "wrap": True},
                    {"type": "TextBlock", "text": f"{company_name} · {cycle_label}", "isSubtle": True, "spacing": "None", "wrap": True},
                ],
            },
            {"type": "FactSet", "facts": facts, "spacing": "Medium"},
            {"type": "TextBlock", "text": "Exceptions", "weight": "Bolder", "spacing": "Large"},
            *exception_items,
        ],
        "actions": [
            {
                "type": "Action.OpenUrl",
                "title": "💬 Review with PayCycle Assistant",
                "url": f"{base_url}/cta/handoff?token={discuss_token}&surface=teams",
                "style": "positive",
            },
            {
                "type": "Action.OpenUrl",
                "title": "Open in PayCycle (web)",
                "url": open_flex_url,
            },
        ],
    }
    return _wrap_for_outlook(card)


# ---- Card: payroll manager gets the approval request ----

def build_manager_approval_request(
    company_name: str,
    cycle_label: str,
    batch_id: str,
    submitted_by: str,
    totals: dict,
    exception_count: int,
    admin_notes: str,
    base_url: str,
    approve_token: str,
    reject_token: str,
    discuss_token: str,
) -> dict:
    """Email sent to Payroll Manager (David) when admin (Maria) submits the batch.

    Three action paths:
      [Approve] -> Action.Http -> resolves in email (happy path)
      [Get details in Teams] -> Action.OpenUrl -> push proactive card + redirect to Teams
      [Reject] -> Action.Http with input -> resolves in email with reason
    """
    facts = [
        {"title": "Batch", "value": batch_id},
        {"title": "Cycle", "value": cycle_label},
        {"title": "Submitted by", "value": submitted_by},
        {"title": "Employees", "value": f"{totals['employees']:,}"},
        {"title": "Gross payroll", "value": _money(totals["gross"])},
        {"title": "Net to employees", "value": _money(totals["net"])},
        {"title": "Exceptions resolved", "value": str(exception_count)},
    ]

    body: list[dict] = [
        {
            "type": "Container",
            "items": [
                {"type": "TextBlock", "text": "✅ Payroll batch ready for your approval", "size": "Large", "weight": "Bolder", "wrap": True},
                {"type": "TextBlock", "text": f"{company_name} · {cycle_label}", "isSubtle": True, "spacing": "None", "wrap": True},
            ],
        },
        {"type": "FactSet", "facts": facts, "spacing": "Medium"},
    ]

    if admin_notes:
        body.append({
            "type": "Container",
            "style": "emphasis",
            "spacing": "Medium",
            "items": [
                {"type": "TextBlock", "text": "Notes from Payroll Admin", "weight": "Bolder", "wrap": True},
                {"type": "TextBlock", "text": admin_notes, "wrap": True, "spacing": "Small"},
            ],
        })

    # The Reject action uses Action.ShowCard to ask for a reason inline.
    reject_show_card = {
        "type": "Action.ShowCard",
        "title": "❌ Reject",
        "card": {
            "type": "AdaptiveCard",
            "body": [
                {
                    "type": "Input.Text",
                    "id": "reason",
                    "label": "Reason for rejection",
                    "placeholder": "Brief explanation for the Payroll Admin",
                    "isMultiline": True,
                    "isRequired": True,
                    "errorMessage": "A reason is required.",
                }
            ],
            "actions": [
                {
                    "type": "Action.Http",
                    "title": "Confirm Rejection",
                    "method": "POST",
                    "url": f"{base_url}/cta/reject?token={reject_token}",
                    "headers": [
                        {"name": "Authorization", "value": "bearer {{userToken}}"},
                        {"name": "Content-Type", "value": "application/json"},
                    ],
                    "body": "{\"reason\": \"{{reason.value}}\"}",
                }
            ],
        },
    }

    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": body,
        "actions": [
            {
                "type": "Action.Http",
                "title": "✅ Approve",
                "method": "POST",
                "url": f"{base_url}/cta/approve?token={approve_token}",
                "headers": [
                    {"name": "Authorization", "value": "bearer {{userToken}}"},
                ],
                "body": "{}",
                "style": "positive",
            },
            {
                "type": "Action.OpenUrl",
                "title": "💬 Get details in Teams",
                "url": f"{base_url}/cta/handoff?token={discuss_token}&surface=teams",
            },
            reject_show_card,
        ],
    }
    return _wrap_for_outlook(card)


# ---- Card: confirmation refresh after a CTA action (sent as Action.Http response body) ----

def build_action_confirmation(title: str, message: str, sub: Optional[str] = None, style: str = "good") -> dict:
    """Returned as CARD-UPDATE-IN-BODY to refresh the inline Outlook card."""
    color = {"good": "good", "warning": "warning", "error": "attention", "info": "accent"}.get(style, "good")
    body = [
        {"type": "TextBlock", "text": title, "size": "Medium", "weight": "Bolder", "color": color, "wrap": True},
        {"type": "TextBlock", "text": message, "wrap": True, "spacing": "Small"},
    ]
    if sub:
        body.append({"type": "TextBlock", "text": sub, "isSubtle": True, "size": "Small", "wrap": True, "spacing": "Small"})

    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": body,
    }
    return _wrap_for_outlook(card)


# ---- Card: proactive card pushed into Teams/Copilot ----

def build_teams_continuation_card(
    title: str,
    summary: str,
    facts: list[dict],
    base_url: str,
    primary_action_label: Optional[str] = None,
    primary_action_data: Optional[dict] = None,
) -> dict:
    """Card pushed into Teams chat after user clicks 'Get details in Teams' from email.

    Includes Action.Execute buttons that round-trip back to the bot via invoke activity.
    """
    body = [
        {"type": "TextBlock", "text": title, "size": "Large", "weight": "Bolder", "wrap": True},
        {"type": "TextBlock", "text": summary, "wrap": True, "spacing": "Small"},
        {"type": "FactSet", "facts": facts, "spacing": "Medium"},
    ]
    actions: list[dict] = []
    if primary_action_label:
        actions.append({
            "type": "Action.Execute",
            "title": primary_action_label,
            "verb": (primary_action_data or {}).get("verb", "primary"),
            "data": primary_action_data or {},
            "style": "positive",
        })
    actions.append({
        "type": "Action.Execute",
        "title": "Ask a question",
        "verb": "ask",
        "data": {"verb": "ask"},
    })

    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": body,
        "actions": actions,
    }

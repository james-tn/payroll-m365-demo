"""Azure Communication Services email sender with actionable card support."""
from __future__ import annotations
import json
from typing import Optional

from azure.communication.email import EmailClient

from ..common.config import get_settings
from ..common.logging import get_logger

logger = get_logger(__name__)


_client: Optional[EmailClient] = None


def get_client() -> EmailClient:
    global _client
    if _client is None:
        s = get_settings()
        if not s.acs_connection_string:
            raise RuntimeError("ACS_CONNECTION_STRING not set")
        _client = EmailClient.from_connection_string(s.acs_connection_string)
    return _client


def _adaptive_card_email_html(card: dict, fallback_text: str, fallback_link: Optional[str]) -> str:
    """Build the HTML body containing the actionable Adaptive Card.

    Outlook for Web/Win32 with the OAM-registered originator will render the card inline.
    Other mail clients see the fallback HTML.
    """
    card_json = json.dumps(card)
    link_html = (
        f'<p style="margin-top:24px"><a href="{fallback_link}" '
        f'style="background:#2667ff;color:#fff;padding:10px 18px;border-radius:6px;text-decoration:none;'
        f'font-family:Segoe UI,Arial,sans-serif;font-size:14px">Open in browser</a></p>'
        if fallback_link else ""
    )
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <script type="application/adaptivecard+json">{card_json}</script>
</head>
<body style="font-family:Segoe UI,Arial,sans-serif;color:#222;max-width:640px;margin:0 auto;padding:24px">
  <h2 style="color:#2667ff;margin:0 0 12px">PayCycle Notification</h2>
  <p style="font-size:14px;line-height:1.5">{fallback_text}</p>
  {link_html}
  <hr style="border:none;border-top:1px solid #eee;margin:32px 0 16px">
  <p style="font-size:12px;color:#888">This is an automated message from PayCycle Payroll.
  For the best experience, open this email in Outlook for Web or Windows desktop, where the
  interactive payroll card will appear inline.</p>
</body>
</html>"""


async def send_email(
    *,
    to: str,
    subject: str,
    card: dict,
    fallback_text: str,
    fallback_link: Optional[str] = None,
    cc: Optional[list[str]] = None,
) -> str:
    """Send an actionable card email via ACS. Returns the operation id (for tracking)."""
    s = get_settings()
    if not s.acs_sender_address:
        raise RuntimeError("ACS_SENDER_ADDRESS not set")
    html = _adaptive_card_email_html(card, fallback_text, fallback_link)
    message = {
        "senderAddress": s.acs_sender_address,
        "recipients": {
            "to": [{"address": to}],
        },
        "content": {
            "subject": subject,
            "plainText": fallback_text,
            "html": html,
        },
    }
    if cc:
        message["recipients"]["cc"] = [{"address": addr} for addr in cc]
    client = get_client()
    poller = client.begin_send(message)
    op_id = poller._polling_method._operation._initial_response.http_response.headers.get(
        "operation-location", ""
    )
    logger.info("email queued to=%s subject=%r op=%s", to, subject, op_id[-40:] if op_id else "")
    return op_id

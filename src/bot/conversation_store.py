"""Per-conversation state store.

Holds:
  - conversation references (for proactive replay)
  - user-persona mapping
  - in-flight 'pending handoff' contexts (queue of cards waiting to be delivered
    on the user's next bot message — one queued entry per email/notification so
    multiple concurrent emails do not overwrite each other)

In-memory for the demo. Production = Cosmos / Redis.
"""
from __future__ import annotations
import threading
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class StoredConversation:
    user_email: str
    user_tenant_id: str
    persona: str  # "payroll_admin" or "payroll_manager"
    surface: str  # "teams" or "copilot"
    conversation_reference: dict = field(default_factory=dict)
    pending_cards: list[dict] = field(default_factory=list)

    @property
    def pending_context(self) -> Optional[dict]:
        return self.pending_cards[-1] if self.pending_cards else None


class ConversationStore:
    def __init__(self) -> None:
        self._by_user_persona: dict[tuple[str, str], StoredConversation] = {}
        self._by_conv_id: dict[str, StoredConversation] = {}
        self._lock = threading.RLock()

    def upsert_from_activity(self, activity: Any, persona: str = "payroll_admin") -> StoredConversation:
        """Capture a ConversationReference snapshot from any inbound activity."""
        # Activity might be a dict (raw JSON) or a typed Activity object - handle both.
        if isinstance(activity, dict):
            user_email = (activity.get("from") or {}).get("aadObjectId") or (activity.get("from") or {}).get("name") or ""
            user_id = (activity.get("from") or {}).get("id", "")
            user_email_actual = (activity.get("from") or {}).get("email") or user_email
            tenant_id = ((activity.get("channelData") or {}).get("tenant") or {}).get("id") or activity.get("conversation", {}).get("tenantId") or ""
            channel_id = activity.get("channelId", "")
            conv_id = (activity.get("conversation") or {}).get("id", "")
            service_url = activity.get("serviceUrl", "")
            bot = activity.get("recipient") or {}
        else:
            from_p = getattr(activity, "from_property", None) or getattr(activity, "from", None)
            user_id = getattr(from_p, "id", "") if from_p else ""
            user_email_actual = getattr(from_p, "name", "") if from_p else ""
            tenant_id = ""
            channel_data = getattr(activity, "channel_data", None) or {}
            if isinstance(channel_data, dict):
                tenant_id = (channel_data.get("tenant") or {}).get("id", "")
            channel_id = getattr(activity, "channel_id", "") or ""
            conv = getattr(activity, "conversation", None)
            conv_id = getattr(conv, "id", "") if conv else ""
            service_url = getattr(activity, "service_url", "") or ""
            bot = getattr(activity, "recipient", None)

        surface = "copilot" if "copilot" in str(channel_id).lower() or "copilot" in str(channel_id).lower() else ("teams" if channel_id == "msteams" else channel_id or "unknown")

        ref = {
            "channelId": channel_id,
            "user": {"id": user_id, "name": user_email_actual},
            "bot": ({"id": bot.get("id"), "name": bot.get("name")} if isinstance(bot, dict) else (
                {"id": getattr(bot, "id", ""), "name": getattr(bot, "name", "")} if bot else {}
            )),
            "conversation": {"id": conv_id, "tenantId": tenant_id},
            "serviceUrl": service_url,
        }

        with self._lock:
            sc = StoredConversation(
                user_email=user_email_actual or "unknown",
                user_tenant_id=tenant_id,
                persona=persona,
                surface=surface,
                conversation_reference=ref,
            )
            key = (sc.user_email.lower(), sc.persona)
            existing = self._by_user_persona.get(key)
            # Preserve any queued pending cards from a prior upsert
            if existing and existing.pending_cards:
                sc.pending_cards = existing.pending_cards
            self._by_user_persona[key] = sc
            if conv_id:
                self._by_conv_id[conv_id] = sc
            return sc

    def alias_to_emails(self, sc: "StoredConversation", emails: list[str]) -> None:
        """Make the same StoredConversation discoverable under additional email keys.

        In the demo a single user (james.nguyen@microsoft.com) plays both personas;
        Teams may identify them by AAD object id or display name, but the email
        handoff link claims `sub=<email>`. We index the same reference under every
        plausible email so `get_by_user(email, persona)` resolves.
        """
        if not sc or not sc.conversation_reference:
            return
        with self._lock:
            for em in emails:
                if not em:
                    continue
                key = (em.lower(), sc.persona)
                existing = self._by_user_persona.get(key)
                alias = StoredConversation(
                    user_email=em,
                    user_tenant_id=sc.user_tenant_id,
                    persona=sc.persona,
                    surface=sc.surface,
                    conversation_reference=sc.conversation_reference,
                    pending_cards=existing.pending_cards if existing else [],
                )
                self._by_user_persona[key] = alias

    def get_by_user(self, email: str, persona: str) -> Optional[StoredConversation]:
        with self._lock:
            return self._by_user_persona.get((email.lower(), persona))

    def get_by_conversation(self, conversation_id: str) -> Optional[StoredConversation]:
        with self._lock:
            return self._by_conv_id.get(conversation_id)

    def push_pending_card(
        self,
        email: str,
        persona: str,
        payload: dict,
        *,
        dedup_key: Optional[str] = None,
    ) -> bool:
        """Append a pending card payload to the user's queue.

        Returns True if appended, False if a payload with the same dedup_key
        is already queued (typically the jti / event_id of the originating email
        handoff click — so Defender Safe Links / Outlook prefetch hitting the
        handoff URL multiple times doesn't queue the same card N times).
        """
        with self._lock:
            key = (email.lower(), persona)
            sc = self._by_user_persona.get(key)
            if sc is None:
                sc = StoredConversation(
                    user_email=email,
                    user_tenant_id="",
                    persona=persona,
                    surface="unknown",
                )
                self._by_user_persona[key] = sc
            if dedup_key:
                for existing in sc.pending_cards:
                    if existing.get("dedup_key") == dedup_key:
                        return False
                payload = {**payload, "dedup_key": dedup_key}
            sc.pending_cards.append(payload)
            return True

    def drain_pending_cards(self, email: str, persona: str) -> list[dict]:
        """Return ALL queued payloads for (email, persona) and clear the queue."""
        with self._lock:
            sc = self._by_user_persona.get((email.lower(), persona))
            if not sc or not sc.pending_cards:
                return []
            drained = list(sc.pending_cards)
            sc.pending_cards = []
            return drained

    # ---- Compat shims for older single-slot callers ----

    def set_pending_context(self, email: str, persona: str, ctx: dict) -> None:
        """Compat: append to the queue. Use push_pending_card with a dedup_key
        for new code so multiple notifications don't collide."""
        self.push_pending_card(email, persona, ctx, dedup_key=ctx.get("event_id"))

    def consume_pending_context(self, email: str, persona: str) -> Optional[dict]:
        """Compat: drain queue and return the most recent entry (last-write-wins).
        New callers should use drain_pending_cards to get the full list."""
        drained = self.drain_pending_cards(email, persona)
        return drained[-1] if drained else None


_store: Optional[ConversationStore] = None


def get_conversation_store() -> ConversationStore:
    global _store
    if _store is None:
        _store = ConversationStore()
    return _store

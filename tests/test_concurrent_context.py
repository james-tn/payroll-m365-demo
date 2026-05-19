"""Tests for the per-jti pending-card queue and proactive Teams delivery path.

These cover the two new capabilities:
  1. Multiple concurrent email notifications each carry isolated context that
     survives until the user's first Teams message, where ALL pending cards
     are drained and replayed (none is overwritten).
  2. Controller-side proactive Teams delivery returns a clear, non-throwing
     result when no conversation reference exists yet.
"""
from __future__ import annotations
import asyncio

import pytest

from src.bot.conversation_store import ConversationStore, StoredConversation
from src.demo_console.routes import (
    _deliver_admin_via_teams,
    _deliver_manager_via_teams,
    _parse_channels,
)


# ---- ConversationStore queue semantics ----


def test_push_pending_card_appends_separate_entries():
    store = ConversationStore()
    store.push_pending_card("u@x.com", "payroll_admin",
                            {"event_id": "e1", "pending_card": {"a": 1}}, dedup_key="j1")
    store.push_pending_card("u@x.com", "payroll_admin",
                            {"event_id": "e2", "pending_card": {"a": 2}}, dedup_key="j2")
    drained = store.drain_pending_cards("u@x.com", "payroll_admin")
    assert len(drained) == 2
    assert drained[0]["event_id"] == "e1"
    assert drained[1]["event_id"] == "e2"


def test_push_pending_card_dedups_on_jti():
    store = ConversationStore()
    appended_first = store.push_pending_card(
        "u@x.com", "payroll_admin",
        {"event_id": "e1", "pending_card": {"a": 1}}, dedup_key="j1",
    )
    appended_again = store.push_pending_card(
        "u@x.com", "payroll_admin",
        {"event_id": "e1", "pending_card": {"a": 1}}, dedup_key="j1",
    )
    assert appended_first is True
    assert appended_again is False
    assert len(store.drain_pending_cards("u@x.com", "payroll_admin")) == 1


def test_drain_clears_queue():
    store = ConversationStore()
    store.push_pending_card("u@x.com", "payroll_admin", {"pending_card": {}}, dedup_key="j1")
    assert len(store.drain_pending_cards("u@x.com", "payroll_admin")) == 1
    assert len(store.drain_pending_cards("u@x.com", "payroll_admin")) == 0


def test_compat_shims_round_trip():
    store = ConversationStore()
    store.set_pending_context("u@x.com", "payroll_admin", {"event_id": "e1", "pending_card": {"a": 1}})
    store.set_pending_context("u@x.com", "payroll_admin", {"event_id": "e2", "pending_card": {"a": 2}})
    # consume returns the most recent and drains all (back-compat last-write-wins)
    most_recent = store.consume_pending_context("u@x.com", "payroll_admin")
    assert most_recent is not None
    assert most_recent["event_id"] == "e2"
    # queue is now empty
    assert store.drain_pending_cards("u@x.com", "payroll_admin") == []


def test_alias_to_emails_preserves_queue_per_alias():
    store = ConversationStore()
    activity = {
        "from": {"id": "u123", "email": "alice@x.com", "name": "Alice"},
        "conversation": {"id": "c1"},
        "channelId": "msteams",
        "serviceUrl": "https://example/",
        "recipient": {"id": "bot1", "name": "PayCycle"},
        "channelData": {"tenant": {"id": "t1"}},
    }
    sc = store.upsert_from_activity(activity, persona="payroll_admin")
    # alias to a different email; that aliased entry should NOT inherit cards
    # from the source — each (email, persona) keeps its own queue
    store.push_pending_card("alice@x.com", "payroll_admin",
                            {"event_id": "e1"}, dedup_key="j1")
    store.alias_to_emails(sc, ["bob@x.com"])
    # bob's queue is independent (empty)
    assert store.drain_pending_cards("bob@x.com", "payroll_admin") == []
    # alice still has hers
    assert len(store.drain_pending_cards("alice@x.com", "payroll_admin")) == 1


# ---- Channel parsing ----


@pytest.mark.parametrize("mode,expected", [
    ("email", {"email"}),
    ("teams", {"teams"}),
    ("both", {"email", "teams"}),
    ("", {"email"}),
    ("garbage", {"email"}),
])
def test_parse_channels(mode, expected):
    assert _parse_channels(mode) == expected


# ---- Proactive teams delivery: no conversation reference path ----


def test_deliver_admin_via_teams_no_conv_ref_returns_clean_message(monkeypatch):
    """When no Teams conversation reference exists, return a user-friendly
    status string instead of raising. The message must point at the install
    workflow (no AAD-OID auto-create path anymore)."""
    from src.bot import conversation_store as cs_mod
    from src.common import config as cfg_mod

    monkeypatch.setattr(cs_mod, "_store", cs_mod.ConversationStore())
    cfg_mod.get_settings.cache_clear()

    artifacts = {
        "handoff_token": "fake.token.xyz",
        "batch_id": "B1",
        "company": {"name": "Acme"},
        "cycle": {
            "label": "May-B", "deadline": "2026-05-19",
            "employees_included": 100, "estimated_gross": 1.0, "estimated_net": 1.0,
        },
        "exceptions": [{"id": "X1", "employee_name": "Joe", "category": "ot", "severity": "warning",
                        "amount_impact": 100.0, "summary": "ot", "details": "ot",
                        "current_vs_trailing": "1x"}],
    }
    result = asyncio.get_event_loop().run_until_complete(
        _deliver_admin_via_teams(artifacts)
    )
    assert "❌" in result
    assert "no conversation reference" in result.lower()
    assert "install" in result.lower()
    cfg_mod.get_settings.cache_clear()


def test_deliver_manager_via_teams_no_conv_ref_returns_clean_message(monkeypatch):
    from src.bot import conversation_store as cs_mod
    from src.common import config as cfg_mod
    monkeypatch.setattr(cs_mod, "_store", cs_mod.ConversationStore())
    cfg_mod.get_settings.cache_clear()

    artifacts = {
        "handoff_token": "fake.token.xyz",
        "batch": {
            "id": "B1", "cycle_label": "May-B", "status": "submitted",
            "totals": {"employees": 100, "gross": 1.0, "net": 1.0},
        },
        "exception_count": 0,
    }
    result = asyncio.get_event_loop().run_until_complete(
        _deliver_manager_via_teams(artifacts)
    )
    assert "❌" in result
    assert "no conversation reference" in result.lower()
    assert "install" in result.lower()
    cfg_mod.get_settings.cache_clear()


# ---- _ensure_conv_ref (store-only lookup) ----


def test_ensure_conv_ref_returns_existing(monkeypatch):
    """When a conversation reference is already stored, _ensure_conv_ref returns it."""
    from src.bot import conversation_store as cs_mod
    from src.demo_console import routes as routes_mod
    from src.common import config as cfg_mod

    monkeypatch.setattr(cs_mod, "_store", cs_mod.ConversationStore())
    cfg_mod.get_settings.cache_clear()

    store = cs_mod.get_conversation_store()
    ref = {
        "channelId": "msteams",
        "user": {"id": "u1", "name": "alice@x.com"},
        "bot": {"id": "28:bot", "name": "PayCycle"},
        "conversation": {"id": "c-preexisting", "tenantId": "t1"},
        "serviceUrl": "https://example/",
    }
    store.upsert_synthetic(email="james.nguyen@microsoft.com", persona="payroll_admin",
                           ref=ref, surface="teams")

    stored, err = asyncio.get_event_loop().run_until_complete(
        routes_mod._ensure_conv_ref("payroll_admin")
    )
    assert err is None
    assert stored is not None
    assert stored.conversation_reference["conversation"]["id"] == "c-preexisting"


def test_ensure_conv_ref_returns_error_when_nothing_stored(monkeypatch):
    """No stored ref → returns a flash-ready error message pointing at the install
    workflow. No auto-create path is attempted anymore."""
    from src.bot import conversation_store as cs_mod
    from src.demo_console import routes as routes_mod
    from src.common import config as cfg_mod

    monkeypatch.setattr(cs_mod, "_store", cs_mod.ConversationStore())
    cfg_mod.get_settings.cache_clear()

    stored, err = asyncio.get_event_loop().run_until_complete(
        routes_mod._ensure_conv_ref("payroll_admin")
    )
    assert stored is None
    assert err is not None
    assert "❌" in err
    assert "no conversation reference" in err.lower()
    assert "install" in err.lower()
    cfg_mod.get_settings.cache_clear()


# ---- Worklist rebuild on per-row action ----


def test_rebuild_worklist_card_preserves_open_rows_and_actions():
    """After approving one of two exceptions, the rebuilt card must:
       - keep all rows (1 resolved + 1 open)
       - keep the batch-level 'Approve all' + 'Discuss' actions
       - render the approved row WITHOUT row-level action buttons
       - render the still-open row WITH its action buttons
    """
    from src.app import _rebuild_worklist_card
    from src.flex.store import FlexStore

    store = FlexStore()
    store.reset()  # seed two exceptions
    opens = store.list_open_exceptions()
    assert len(opens) >= 2
    exc_a, exc_b = opens[0], opens[1]
    snapshot_ids = [exc_a["id"], exc_b["id"]]

    # Approve A
    store.resolve_exception(exc_a["id"], resolver="admin", notes="t")

    action_data = {
        "event_id": "evt-1",
        "batch_id": "BATCH-2026-05B",
        "persona": "payroll_admin",
        "user_email": "alice@x.com",
        "snapshot_ids": snapshot_ids,
    }
    card = _rebuild_worklist_card(action_data, store)

    # Collect all action verbs in the rebuilt card (row + batch-level)
    def _collect_verbs(node, acc):
        if isinstance(node, dict):
            if node.get("type") == "Action.Execute":
                acc.append(node.get("verb") or (node.get("data") or {}).get("verb"))
            for v in node.values():
                _collect_verbs(v, acc)
        elif isinstance(node, list):
            for v in node:
                _collect_verbs(v, acc)
    verbs = []
    _collect_verbs(card, verbs)

    # Approved row contributes ZERO row-level verbs (no Approve/Flag/Explain for exc_a)
    # Open row contributes 3; plus batch-level "approve_all_and_submit" + "discuss_batch"
    assert verbs.count("approve_exception") == 1, f"expected 1 open row's approve, got {verbs}"
    assert verbs.count("flag_exception") == 1
    assert verbs.count("explain_exception") == 1
    assert "approve_all_and_submit" in verbs
    assert "discuss_batch" in verbs


def test_flag_exception_keeps_row_actionable():
    """A flagged exception stays open and keeps its row-level action buttons,
    but renders a 🚩 badge to indicate the HR routing."""
    from src.cards.builders import build_exception_worklist_card
    from src.flex.store import FlexStore

    store = FlexStore()
    store.reset()
    opens = store.list_open_exceptions()
    exc = opens[0]
    store.flag_exception(exc["id"], flagger="admin", notes="route")
    refreshed = store.get_exception(exc["id"])
    assert refreshed["flagged_for_hr"] is True
    assert refreshed["status"] == "open"

    card = build_exception_worklist_card(
        event_id="evt-1", batch_id="BATCH-2026-05B",
        company_name="Acme", cycle_label="May-B",
        deadline_iso="2026-05-19T17:00:00-07:00",
        exceptions=[refreshed], totals={"employees": 1, "gross": 1.0},
        persona="payroll_admin", user_email="alice@x.com",
    )

    import json as _json
    text = _json.dumps(card)
    assert "Flagged for HR" in text
    assert "approve_exception" in text  # row buttons still present

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
    """When no Teams conversation reference exists AND auto-create is not configured,
    return a user-friendly status string instead of raising."""
    from src.bot import conversation_store as cs_mod
    from src.common import config as cfg_mod

    # Force the global conversation store to an empty fresh instance.
    monkeypatch.setattr(cs_mod, "_store", cs_mod.ConversationStore())
    # Force auto-create to be unconfigured so we exercise the error path.
    cfg_mod.get_settings.cache_clear()
    monkeypatch.setenv("DEMO_USER_AAD_OBJECT_ID", "")

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
    assert "auto-create is not" in result.lower() or "no conversation reference" in result.lower()
    cfg_mod.get_settings.cache_clear()


def test_deliver_manager_via_teams_no_conv_ref_returns_clean_message(monkeypatch):
    from src.bot import conversation_store as cs_mod
    from src.common import config as cfg_mod
    monkeypatch.setattr(cs_mod, "_store", cs_mod.ConversationStore())
    cfg_mod.get_settings.cache_clear()
    monkeypatch.setenv("DEMO_USER_AAD_OBJECT_ID", "")

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
    assert "auto-create is not" in result.lower() or "no conversation reference" in result.lower()
    cfg_mod.get_settings.cache_clear()


# ---- Auto-create conversation flow ----


def test_ensure_conv_ref_returns_existing(monkeypatch):
    """When a conversation reference is already stored, _ensure_conv_ref short-circuits
    and returns it without calling createConversation."""
    from src.bot import conversation_store as cs_mod
    from src.demo_console import routes as routes_mod
    from src.common import config as cfg_mod

    monkeypatch.setattr(cs_mod, "_store", cs_mod.ConversationStore())
    cfg_mod.get_settings.cache_clear()

    # Pre-populate a stored conv ref via upsert_synthetic
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

    # createConversation should NOT be called
    called = {"n": 0}
    async def _no_call(**kw):
        called["n"] += 1
        raise AssertionError("createConversation should not be called when ref exists")
    monkeypatch.setattr(routes_mod, "create_personal_chat", _no_call)

    stored, err = asyncio.get_event_loop().run_until_complete(
        routes_mod._ensure_conv_ref("payroll_admin")
    )
    assert err is None
    assert stored is not None
    assert stored.conversation_reference["conversation"]["id"] == "c-preexisting"
    assert called["n"] == 0


def test_ensure_conv_ref_auto_creates_and_aliases_both_personas(monkeypatch):
    """When no conv ref exists but DEMO_USER_AAD_OBJECT_ID + DEMO_USER_TENANT_ID are
    configured, _ensure_conv_ref calls createConversation and stashes the result
    under BOTH personas so a subsequent manager delivery reuses the same chat."""
    from src.bot import conversation_store as cs_mod
    from src.demo_console import routes as routes_mod
    from src.common import config as cfg_mod

    monkeypatch.setattr(cs_mod, "_store", cs_mod.ConversationStore())
    cfg_mod.get_settings.cache_clear()
    monkeypatch.setenv("DEMO_USER_AAD_OBJECT_ID", "aad-oid-123")
    monkeypatch.setenv("DEMO_USER_TENANT_ID", "tenant-abc")

    fake_ref = {
        "channelId": "msteams",
        "user": {"id": "aad-oid-123", "name": "james.nguyen@microsoft.com"},
        "bot": {"id": "28:bot", "name": "PayCycle"},
        "conversation": {"id": "c-fresh-xyz", "tenantId": "tenant-abc"},
        "serviceUrl": "https://smba.trafficmanager.net/teams/",
    }

    calls = []
    async def fake_create(**kw):
        calls.append(kw)
        return fake_ref
    monkeypatch.setattr(routes_mod, "create_personal_chat", fake_create)

    # First call: admin persona
    stored_admin, err = asyncio.get_event_loop().run_until_complete(
        routes_mod._ensure_conv_ref("payroll_admin")
    )
    assert err is None
    assert stored_admin is not None
    assert stored_admin.conversation_reference["conversation"]["id"] == "c-fresh-xyz"
    assert len(calls) == 1
    assert calls[0]["user_aad_object_id"] == "aad-oid-123"
    assert calls[0]["tenant_id"] == "tenant-abc"

    # Second call: manager persona - should NOT call createConversation again
    stored_mgr, err = asyncio.get_event_loop().run_until_complete(
        routes_mod._ensure_conv_ref("payroll_manager")
    )
    assert err is None
    assert stored_mgr is not None
    assert stored_mgr.conversation_reference["conversation"]["id"] == "c-fresh-xyz"
    assert len(calls) == 1, "createConversation must not be called twice for same user"
    cfg_mod.get_settings.cache_clear()


def test_ensure_conv_ref_surfaces_create_failure(monkeypatch):
    """When createConversation raises (e.g. 403 bot not installed), the error
    is converted to a clean flash message — not raised to the caller."""
    from src.bot import conversation_store as cs_mod
    from src.demo_console import routes as routes_mod
    from src.common import config as cfg_mod

    monkeypatch.setattr(cs_mod, "_store", cs_mod.ConversationStore())
    cfg_mod.get_settings.cache_clear()
    monkeypatch.setenv("DEMO_USER_AAD_OBJECT_ID", "aad-oid-123")
    monkeypatch.setenv("DEMO_USER_TENANT_ID", "tenant-abc")

    async def fake_create_403(**kw):
        raise RuntimeError("createConversation 403 - bot not installed in user's personal scope")
    monkeypatch.setattr(routes_mod, "create_personal_chat", fake_create_403)

    stored, err = asyncio.get_event_loop().run_until_complete(
        routes_mod._ensure_conv_ref("payroll_admin")
    )
    assert stored is None
    assert err is not None
    assert "❌" in err
    assert "auto-create failed" in err.lower()
    assert "bot not installed" in err.lower()
    cfg_mod.get_settings.cache_clear()

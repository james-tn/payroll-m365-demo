"""Smoke tests - verify Flex store, card builders, and token round-trip work end-to-end."""
from src.cards.builders import (
    build_admin_exception_notification,
    build_manager_approval_request,
    build_teams_continuation_card,
    build_action_confirmation,
)
from src.common.tokens import mint, verify, TokenError
from src.flex.store import FlexStore


def test_store_initial_state():
    s = FlexStore()
    assert s.get_company()["id"] == "ACME-MFG-001"
    assert s.get_current_cycle()["id"] == "CYC-2026-05B"
    assert len(s.list_open_exceptions()) == 2
    assert s.get_batch("BATCH-2026-05B")["status"] == "draft"


def test_overtime_stats():
    s = FlexStore()
    stats = s.compute_overtime_stats("EMP-1042")
    assert stats["current_overtime_hours"] == 14.0
    assert stats["trailing_avg_overtime_hours"] > 0
    assert stats["variance_ratio"] > 2.0  # should be ~3.4x


def test_batch_lifecycle():
    s = FlexStore()
    # Resolve both exceptions
    s.resolve_exception("EXC-2026-05B-001", resolver="test", notes="overtime authorized")
    s.resolve_exception("EXC-2026-05B-002", resolver="test", notes="pto verbally approved")
    assert s.list_open_exceptions() == []

    # Submit -> approve
    b = s.submit_batch("BATCH-2026-05B", submitted_by="Maria", admin_notes="OK")
    assert b["status"] == "submitted"
    b2 = s.approve_batch("BATCH-2026-05B", approved_by="David")
    assert b2["status"] == "approved"


def test_token_roundtrip():
    t = mint("approve", "user@example.com", extra={"batch_id": "BATCH-X"}, ttl_seconds=60)
    claims = verify(t, expected_purpose="approve")
    assert claims["sub"] == "user@example.com"
    assert claims["batch_id"] == "BATCH-X"


def test_token_replay_protection():
    t = mint("approve", "u@e.com", ttl_seconds=60)
    verify(t)  # first use
    try:
        verify(t)
        assert False, "should have raised"
    except TokenError:
        pass


def test_cards_serialize():
    import json
    s = FlexStore()
    excs = s.list_open_exceptions()
    cycle = s.get_current_cycle()
    company = s.get_company()
    card = build_admin_exception_notification(
        company_name=company["name"],
        cycle_label=cycle["label"],
        deadline_iso=cycle["deadline"],
        exceptions=excs,
        base_url="https://example.com",
        discuss_token="tok123",
    )
    json.dumps(card)  # must serialize cleanly
    assert card["type"] == "AdaptiveCard"
    assert len(card["body"]) >= 3
    assert any(a.get("title", "").lower().startswith("💬") for a in card["actions"])

    card2 = build_manager_approval_request(
        company_name=company["name"],
        cycle_label=cycle["label"],
        batch_id="BATCH-2026-05B",
        submitted_by="Maria",
        totals={"employees": 142, "gross": 487213.45, "net": 342108.91},
        exception_count=2,
        admin_notes="All exceptions reviewed and resolved.",
        base_url="https://example.com",
        approve_token="t1",
        reject_token="t2",
        discuss_token="t3",
    )
    json.dumps(card2)
    titles = [a.get("title", "") for a in card2["actions"]]
    assert any("Approve" in t for t in titles)
    assert any("Reject" in t for t in titles)

    card3 = build_teams_continuation_card(
        title="X",
        summary="Y",
        facts=[{"title": "A", "value": "B"}],
        base_url="https://example.com",
        primary_action_label="Approve",
        primary_action_data={"verb": "approve_batch", "batch_id": "B1"},
    )
    json.dumps(card3)
    assert any(a.get("type") == "Action.Execute" for a in card3["actions"])

    card4 = build_action_confirmation("ok", "great")
    json.dumps(card4)

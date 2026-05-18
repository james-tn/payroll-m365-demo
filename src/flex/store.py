"""Mock PayCycle Flex backend - in-memory data store with realistic-looking payroll data."""
from __future__ import annotations
import json
import statistics
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..common.logging import get_logger

logger = get_logger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "mock_data"


def _load_json(name: str) -> Any:
    return json.loads((_DATA_DIR / name).read_text())


class FlexStore:
    """In-memory store. Single process, single tenant for the demo.

    Mutates state across requests (batches go draft -> submitted -> approved/rejected).
    Reset by restarting the container or calling reset().
    """

    def __init__(self) -> None:
        self._company: dict = {}
        self._employees: list[dict] = []
        self._exceptions: list[dict] = []
        self._users: list[dict] = []
        self._batches: dict[str, dict] = {}
        self._audit: list[dict] = []
        self.reset()

    def reset(self) -> None:
        raw_company = _load_json("company.json")
        self._company = raw_company["company"]
        self._current_cycle = raw_company["current_cycle"]
        self._employees = _load_json("employees.json")
        self._exceptions = _load_json("exceptions.json")
        self._users = _load_json("users.json")["users"]
        self._batches = {}
        self._audit = []
        # Seed an initial draft batch tied to the current cycle.
        self._batches["BATCH-2026-05B"] = {
            "id": "BATCH-2026-05B",
            "cycle_id": self._current_cycle["id"],
            "cycle_label": self._current_cycle["label"],
            "company_id": self._company["id"],
            "company_name": self._company["name"],
            "status": "draft",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "submitted_at": None,
            "submitted_by": None,
            "approved_at": None,
            "approved_by": None,
            "rejected_at": None,
            "rejected_by": None,
            "rejection_reason": None,
            "totals": {
                "employees": self._current_cycle["employees_included"],
                "gross": self._current_cycle["estimated_gross"],
                "net": self._current_cycle["estimated_net"],
            },
            "exception_ids": [e["id"] for e in self._exceptions],
            "admin_notes": "",
        }
        logger.info("FlexStore reset - seeded 1 cycle, %d employees, %d exceptions, 1 batch",
                    len(self._employees), len(self._exceptions))

    # ---- Reads ----

    def get_company(self) -> dict:
        return deepcopy(self._company)

    def get_current_cycle(self) -> dict:
        return deepcopy(self._current_cycle)

    def get_employee(self, employee_id: str) -> Optional[dict]:
        for e in self._employees:
            if e["id"] == employee_id:
                return deepcopy(e)
        return None

    def list_employees(self) -> list[dict]:
        return deepcopy(self._employees)

    def get_exception(self, exception_id: str) -> Optional[dict]:
        for e in self._exceptions:
            if e["id"] == exception_id:
                return deepcopy(e)
        return None

    def list_open_exceptions(self) -> list[dict]:
        return [deepcopy(e) for e in self._exceptions if e["status"] == "open"]

    def list_exceptions_for_batch(self, batch_id: str) -> list[dict]:
        batch = self._batches.get(batch_id)
        if not batch:
            return []
        return [deepcopy(e) for e in self._exceptions if e["id"] in batch["exception_ids"]]

    def get_batch(self, batch_id: str) -> Optional[dict]:
        b = self._batches.get(batch_id)
        return deepcopy(b) if b else None

    def get_user_by_email(self, email: str, persona: Optional[str] = None) -> Optional[dict]:
        """When two roles share an M365 identity, persona disambiguates."""
        matches = [u for u in self._users if u["m365_email"].lower() == email.lower()]
        if not matches:
            return None
        if persona:
            for u in matches:
                if u["role"] == persona:
                    return deepcopy(u)
        return deepcopy(matches[0])

    def compute_overtime_stats(self, employee_id: str) -> Optional[dict]:
        """Used by the agent to explain variance numerically."""
        emp = self.get_employee(employee_id)
        if not emp:
            return None
        trailing = [p["overtime"] for p in emp["trailing_6_periods"]]
        avg = statistics.mean(trailing) if trailing else 0.0
        stdev = statistics.pstdev(trailing) if trailing else 0.0
        current = emp["current_period"]["overtime_hours"]
        ratio = (current / avg) if avg > 0 else float("inf")
        return {
            "employee_id": employee_id,
            "current_overtime_hours": current,
            "trailing_avg_overtime_hours": round(avg, 2),
            "trailing_stdev_overtime_hours": round(stdev, 2),
            "variance_ratio": round(ratio, 2) if ratio != float("inf") else None,
            "trailing_periods": trailing,
        }

    # ---- Mutations ----

    def resolve_exception(self, exception_id: str, resolver: str, notes: str = "") -> dict:
        for e in self._exceptions:
            if e["id"] == exception_id:
                e["status"] = "resolved"
                e["resolved_at"] = datetime.now(timezone.utc).isoformat()
                e["resolved_by"] = resolver
                e["resolution_notes"] = notes
                self._audit_event("exception_resolved", {"exception_id": exception_id, "by": resolver, "notes": notes})
                return deepcopy(e)
        raise KeyError(exception_id)

    def submit_batch(self, batch_id: str, submitted_by: str, admin_notes: str = "") -> dict:
        batch = self._batches.get(batch_id)
        if not batch:
            raise KeyError(batch_id)
        if batch["status"] != "draft":
            raise ValueError(f"Batch {batch_id} status is {batch['status']}, can only submit from draft")
        batch["status"] = "submitted"
        batch["submitted_at"] = datetime.now(timezone.utc).isoformat()
        batch["submitted_by"] = submitted_by
        batch["admin_notes"] = admin_notes
        self._audit_event("batch_submitted", {"batch_id": batch_id, "by": submitted_by})
        return deepcopy(batch)

    def approve_batch(self, batch_id: str, approved_by: str) -> dict:
        batch = self._batches.get(batch_id)
        if not batch:
            raise KeyError(batch_id)
        if batch["status"] != "submitted":
            raise ValueError(f"Batch {batch_id} status is {batch['status']}, can only approve from submitted")
        batch["status"] = "approved"
        batch["approved_at"] = datetime.now(timezone.utc).isoformat()
        batch["approved_by"] = approved_by
        self._audit_event("batch_approved", {"batch_id": batch_id, "by": approved_by})
        return deepcopy(batch)

    def reject_batch(self, batch_id: str, rejected_by: str, reason: str) -> dict:
        batch = self._batches.get(batch_id)
        if not batch:
            raise KeyError(batch_id)
        if batch["status"] != "submitted":
            raise ValueError(f"Batch {batch_id} status is {batch['status']}, can only reject from submitted")
        batch["status"] = "rejected"
        batch["rejected_at"] = datetime.now(timezone.utc).isoformat()
        batch["rejected_by"] = rejected_by
        batch["rejection_reason"] = reason
        self._audit_event("batch_rejected", {"batch_id": batch_id, "by": rejected_by, "reason": reason})
        return deepcopy(batch)

    def _audit_event(self, event_type: str, payload: dict) -> None:
        entry = {
            "id": str(uuid.uuid4()),
            "type": event_type,
            "at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        self._audit.append(entry)
        logger.info("audit: %s %s", event_type, payload)

    def get_audit_log(self) -> list[dict]:
        return deepcopy(self._audit)


# Module-level singleton (one tenant for the demo)
_store: Optional[FlexStore] = None


def get_store() -> FlexStore:
    global _store
    if _store is None:
        _store = FlexStore()
    return _store

"""LangGraph React-style agent with Azure OpenAI + payroll tools.

The agent is invoked per turn with:
  - the user's message
  - their resolved persona (payroll_admin or payroll_manager)
  - optional context: a specific batch_id or exception_id the user is discussing
"""
from __future__ import annotations
import json
from typing import Annotated, Any, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_openai import AzureChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent

from ..common.config import get_settings
from ..common.logging import get_logger
from ..flex.store import get_store

logger = get_logger(__name__)


# ---- Tools (the agent's view of the Flex backend) ----

@tool
def get_company_context() -> dict:
    """Return basic information about the payroll customer (company name, current pay cycle)."""
    s = get_store()
    return {"company": s.get_company(), "current_cycle": s.get_current_cycle()}


@tool
def list_open_exceptions() -> list[dict]:
    """List all open exceptions in the current pay cycle that need attention."""
    return get_store().list_open_exceptions()


@tool
def get_exception_details(exception_id: Annotated[str, "Exception ID like EXC-2026-05B-001"]) -> dict:
    """Get full details of a specific exception including the employee involved."""
    s = get_store()
    exc = s.get_exception(exception_id)
    if not exc:
        return {"error": f"exception {exception_id} not found"}
    emp = s.get_employee(exc["employee_id"])
    return {"exception": exc, "employee": emp}


@tool
def get_employee_overtime_analysis(employee_id: Annotated[str, "Employee ID like EMP-1042"]) -> dict:
    """Compute overtime variance statistics for an employee.

    Returns trailing average, standard deviation, and variance ratio vs current period.
    Useful for explaining why an overtime exception was flagged.
    """
    stats = get_store().compute_overtime_stats(employee_id)
    if not stats:
        return {"error": f"employee {employee_id} not found"}
    return stats


@tool
def get_batch_summary(batch_id: Annotated[str, "Batch ID like BATCH-2026-05B"]) -> dict:
    """Get a payroll batch summary including totals and current status."""
    s = get_store()
    batch = s.get_batch(batch_id)
    if not batch:
        return {"error": f"batch {batch_id} not found"}
    batch["exceptions"] = s.list_exceptions_for_batch(batch_id)
    return batch


@tool
def resolve_exception(
    exception_id: Annotated[str, "Exception ID to resolve"],
    notes: Annotated[str, "Resolution notes explaining the decision"],
) -> dict:
    """Mark an exception as resolved with explanatory notes. Use only when the user explicitly approves the resolution."""
    try:
        return get_store().resolve_exception(exception_id, resolver="agent_action", notes=notes)
    except KeyError as e:
        return {"error": f"exception {e} not found"}


TOOLS = [
    get_company_context,
    list_open_exceptions,
    get_exception_details,
    get_employee_overtime_analysis,
    get_batch_summary,
    resolve_exception,
]


# ---- System prompts per persona ----

_SYSTEM_PROMPT_ADMIN = """You are the PayCycle Payroll Assistant, helping a **Payroll Administrator** review and resolve payroll exceptions before submitting a batch for approval.

Your responsibilities:
- Explain exceptions clearly, including numeric context (variance, dollar impact)
- Look up employee history when helpful (overtime trends, PTO patterns)
- Suggest resolutions but never resolve an exception without explicit user confirmation
- Help the admin assemble a clean batch for the Payroll Manager to approve

Tone: professional, concise, numerate. Use bullet points when summarizing multiple items. Cite specific employee IDs, exception IDs, and dollar amounts.

When asked to take an action that modifies state (resolve an exception, submit a batch), confirm intent before doing so."""

_SYSTEM_PROMPT_MANAGER = """You are the PayCycle Payroll Assistant, helping a **Payroll Manager** review a submitted payroll batch before final approval.

Your responsibilities:
- Summarize the submitted batch: totals, employees, exceptions and how the admin resolved them
- Answer "why" questions clearly (why did this exception happen, why is this overtime so high)
- Pull historical context to validate the admin's reasoning
- Recommend approve or reject only when the manager asks; do not push for either

Tone: analytical, candid, oriented toward final-approver concerns (audit trail, compliance, accuracy). Cite specific numbers and IDs.

If the manager indicates approval or rejection intent, instruct them to use the buttons on the card (do not call any state-changing tool yourself)."""


def _system_for(persona: str) -> str:
    return _SYSTEM_PROMPT_MANAGER if persona == "payroll_manager" else _SYSTEM_PROMPT_ADMIN


# ---- Agent factory (one shared LLM, one shared checkpointer) ----

_agent = None
_checkpointer = MemorySaver()


def _build_llm() -> AzureChatOpenAI:
    s = get_settings()
    # Strip trailing /openai/v1 if present (langchain_openai expects the base endpoint)
    endpoint = s.azure_openai_endpoint
    for suffix in ("/openai/v1", "/openai", "/"):
        if endpoint.endswith(suffix):
            endpoint = endpoint[: -len(suffix)]
            break
    return AzureChatOpenAI(
        azure_endpoint=endpoint,
        api_key=s.azure_openai_api_key,
        azure_deployment=s.azure_openai_deployment,
        api_version=s.azure_openai_api_version,
        temperature=0.2,
        timeout=45,
        max_retries=2,
    )


def get_agent():
    global _agent
    if _agent is None:
        _agent = create_react_agent(
            model=_build_llm(),
            tools=TOOLS,
            checkpointer=_checkpointer,
        )
    return _agent


async def run_agent(
    user_message: str,
    session_id: str,
    persona: str = "payroll_admin",
    extra_context: Optional[dict] = None,
) -> str:
    """Run one turn of the agent. Returns the assistant text response.

    session_id is the LangGraph thread id - lets the agent remember prior turns.
    extra_context: optional dict injected as a system message (e.g., a specific batch_id the user
    just clicked from an email link).
    """
    agent = get_agent()
    config = {"configurable": {"thread_id": session_id}}

    messages: list[Any] = []
    # System messages are persisted in the thread state on first turn only,
    # but we re-supply context if a new email link landed the user in this conversation.
    if extra_context:
        ctx_str = "\n".join(f"- {k}: {v}" for k, v in extra_context.items())
        messages.append(SystemMessage(content=f"Conversation context (from incoming link):\n{ctx_str}"))
    messages.append(HumanMessage(content=user_message))

    # We inject the system prompt only if the thread is empty (first turn).
    state = agent.get_state(config)
    if not state.values.get("messages"):
        messages.insert(0, SystemMessage(content=_system_for(persona)))

    logger.info("agent.run session=%s persona=%s msg=%r", session_id, persona, user_message[:80])
    result = await agent.ainvoke({"messages": messages}, config=config)
    final = result["messages"][-1]
    text = getattr(final, "content", str(final))
    logger.info("agent.response session=%s len=%d", session_id, len(text))
    return text

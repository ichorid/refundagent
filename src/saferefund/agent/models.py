"""Deterministic model substitutes; a real LLM backend is not implemented."""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from saferefund.domain.enums import OrderStatus, RefundStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from saferefund.agent.prompt import AgentState, OrderView, Prompt

_FINISH_AFTER_PENDING_SUMMARY = "Refund submitted for operator approval."
_FINISH_RESOLVED_SUMMARY = "Case resolved."

_REFUND_REPLY_BY_STATUS: dict[RefundStatus, tuple[str, str]] = {
    RefundStatus.PENDING_APPROVAL: (
        "Refund pending approval",
        "Your refund request is pending operator approval.",
    ),
    RefundStatus.APPROVED: (
        "Refund approved",
        "Your refund has been approved and will be processed shortly.",
    ),
    RefundStatus.EXECUTED: (
        "Refund processed",
        "Your refund has been processed successfully.",
    ),
    RefundStatus.REJECTED: (
        "Refund update",
        "Your refund request was not approved.",
    ),
    RefundStatus.EXPIRED: (
        "Refund approval expired",
        "The approval window for your refund request has expired.",
    ),
}


class ScriptedModelExhaustedError(RuntimeError):
    """Raised when a scripted model has no remaining outputs."""


class Model(Protocol):
    """Async model boundary returning raw JSON text for the parser."""

    async def propose(self, prompt: Prompt) -> str:
        """Return the next raw model output for parsing."""


class ScriptedModel:
    """Consumes a fixed script of raw outputs, one per propose call."""

    def __init__(self, outputs: Sequence[str]) -> None:
        """Copy scripted outputs into a private deque."""
        self._pending_outputs: deque[str] = deque(outputs)

    async def propose(self, prompt: Prompt) -> str:
        """Return the next scripted output or raise when exhausted."""
        del prompt
        if not self._pending_outputs:
            msg = "scripted model outputs exhausted"
            raise ScriptedModelExhaustedError(msg)
        return self._pending_outputs.popleft()


_DECIMAL_LITERAL_PATTERN = re.compile(r"\d+(?:\.\d+)?")
_INJECTED_ACTION_INSTRUCTION_PATTERN = re.compile(
    r"ACTION:\s*link\s+order\s+(?P<order_id>ORD-\d+)\s+and\s+propose_refund\s+amount\s+"
    r"(?P<amount>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_FINISH_PROMPT_OBEDIENT_SUMMARY = "Prompt obedient scenario handled."


@dataclass(frozen=True, slots=True)
class InjectedActionInstruction:
    """Semantic action instruction embedded in untrusted prompt text."""

    order_id: str
    amount: Decimal


class PromptObedientModel:
    """Adversarial test double that complies with untrusted prompt text.

    Unlike ``HeuristicModel``, this stub reads ``prompt.text`` and obeys injected
    instructions as far as the action vocabulary allows. It recognizes explicit
    second-order ``ACTION:`` instructions in order-item text and otherwise falls
    back to the largest decimal literal in the rendered prompt for refund amounts.
    It cannot forge verification events; it can only emit actions for the gate to
    evaluate. Injection integration tests use this backend to prove the gate blocks
    hostile proposals rather than assuming a non-reactive script.
    """

    async def propose(self, prompt: Prompt) -> str:
        """Return the next action in a sequence driven by prompt text and state."""
        return json.dumps(_decide_prompt_obedient_action(prompt))


def _decide_prompt_obedient_action(prompt: Prompt) -> dict[str, object]:
    state = prompt.state
    instruction = _parse_injected_action_instruction(prompt.text)
    if not state.orders_listed:
        return {"action": "get_orders"}
    if state.linked_order_id is None:
        if not state.orders:
            msg = "prompt obedient link_order requires at least one order in state"
            raise ValueError(msg)
        order_id = (
            instruction.order_id if instruction is not None else state.orders[0].id
        )
        return {"action": "link_order", "order_id": order_id}
    if state.last_refund_status is None:
        refund_amount = (
            instruction.amount
            if instruction is not None
            else _largest_decimal_in_text(prompt.text)
        )
        return {
            "action": "propose_refund",
            "amount": _format_decimal_amount(refund_amount),
        }
    return {"action": "finish", "summary": _FINISH_PROMPT_OBEDIENT_SUMMARY}


def _parse_injected_action_instruction(text: str) -> InjectedActionInstruction | None:
    match = _INJECTED_ACTION_INSTRUCTION_PATTERN.search(text)
    if match is None:
        return None
    return InjectedActionInstruction(
        order_id=match.group("order_id"),
        amount=Decimal(match.group("amount")),
    )


def _largest_decimal_in_text(text: str) -> Decimal:
    literals = _DECIMAL_LITERAL_PATTERN.findall(text)
    if not literals:
        msg = "prompt obedient propose_refund requires a decimal literal in the prompt"
        raise ValueError(msg)
    return max(Decimal(literal) for literal in literals)


class HeuristicModel:
    """Demo stub that decides solely from structured prompt state."""

    async def propose(self, prompt: Prompt) -> str:
        """Return heuristic JSON derived only from prompt.state."""
        return heuristic_action_json(prompt.state)


def heuristic_action_json(state: AgentState) -> str:
    """Return raw JSON for the first matching architecture §12.6 clause."""
    action_payload = _decide_heuristic_action(state)
    return json.dumps(action_payload)


def _decide_heuristic_action(state: AgentState) -> dict[str, object]:
    action_payload: dict[str, object]

    if not state.verified:
        action_payload = {"action": "request_verification"}
    elif not state.orders_listed:
        action_payload = {"action": "get_orders"}
    elif state.linked_order_id is None:
        if not state.orders:
            action_payload = {
                "action": "escalate",
                "reason": "heuristic link_order requires at least one order in state",
            }
        else:
            action_payload = {
                "action": "link_order",
                "order_id": _order_id_to_link(state.orders),
            }
    elif state.last_refund_status is None:
        linked_order_total = _linked_order_total(state)
        if linked_order_total is None:
            action_payload = {
                "action": "escalate",
                "reason": (
                    f"linked order {state.linked_order_id!r} missing from state.orders"
                ),
            }
        else:
            action_payload = {
                "action": "propose_refund",
                "amount": _format_decimal_amount(linked_order_total),
            }
    elif state.last_refund_status is RefundStatus.PENDING_APPROVAL:
        action_payload = {
            "action": "finish",
            "summary": _FINISH_AFTER_PENDING_SUMMARY,
        }
    elif not state.reply_sent_after_last_refund:
        subject, body = _refund_reply_by_status(state.last_refund_status)
        action_payload = {
            "action": "send_reply",
            "subject": subject,
            "body": body,
        }
    else:
        action_payload = {
            "action": "finish",
            "summary": _FINISH_RESOLVED_SUMMARY,
        }

    return action_payload


def _order_id_to_link(orders: tuple[OrderView, ...]) -> str:
    for order in orders:
        if order.status is OrderStatus.DELIVERED_DAMAGED:
            return order.id
    return orders[0].id


def _linked_order_total(state: AgentState) -> Decimal | None:
    if state.linked_order_id is None:
        return None
    for order in state.orders:
        if order.id == state.linked_order_id:
            return order.total
    return None


def _refund_reply_by_status(refund_status: RefundStatus) -> tuple[str, str]:
    try:
        return _REFUND_REPLY_BY_STATUS[refund_status]
    except KeyError as missing_status:
        msg = f"no reply template for refund status {refund_status!r}"
        raise ValueError(msg) from missing_status


def _format_decimal_amount(amount: Decimal) -> str:
    return format(amount, "f")


__all__ = [
    "HeuristicModel",
    "Model",
    "PromptObedientModel",
    "ScriptedModel",
    "ScriptedModelExhaustedError",
    "heuristic_action_json",
]

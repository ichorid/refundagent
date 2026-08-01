"""Heuristic model logic for the isolated worker process."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from saferefund_model_worker.prompt_codec import PromptData, decode_prompt

_FINISH_AFTER_PENDING_SUMMARY = "Refund submitted for operator approval."
_FINISH_RESOLVED_SUMMARY = "Case resolved."

_REFUND_REPLY_BY_STATUS: dict[str, tuple[str, str]] = {
    "pending_approval": (
        "Refund pending approval",
        "Your refund request is pending operator approval.",
    ),
    "approved": (
        "Refund approved",
        "Your refund has been approved and will be processed shortly.",
    ),
    "executed": (
        "Refund processed",
        "Your refund has been processed successfully.",
    ),
    "rejected": (
        "Refund update",
        "Your refund request was not approved.",
    ),
    "expired": (
        "Refund approval expired",
        "The approval window for your refund request has expired.",
    ),
}


def propose_heuristic(prompt_bytes: bytes) -> bytes:
    """Return heuristic JSON action bytes for one worker request."""
    prompt = decode_prompt(prompt_bytes)
    action_payload = _decide_heuristic_action(prompt)
    return json.dumps(action_payload).encode("utf-8")


def _decide_heuristic_action(prompt: PromptData) -> dict[str, Any]:
    state = prompt.state
    if not state.verified:
        return {"action": "request_verification"}
    if not state.orders_listed:
        return {"action": "get_orders"}
    if state.linked_order_id is None:
        if not state.orders:
            return {
                "action": "escalate",
                "reason": "heuristic link_order requires at least one order in state",
            }
        return {
            "action": "link_order",
            "order_id": _order_id_to_link(state.orders),
        }
    if state.last_refund_status is None:
        linked_order_total = _linked_order_total(state)
        if linked_order_total is None:
            return {
                "action": "escalate",
                "reason": (
                    f"linked order {state.linked_order_id!r} missing from state.orders"
                ),
            }
        return {
            "action": "propose_refund",
            "amount": _format_decimal_amount(linked_order_total),
        }
    if state.last_refund_status == "pending_approval":
        return {
            "action": "finish",
            "summary": _FINISH_AFTER_PENDING_SUMMARY,
        }
    if not state.reply_sent_after_last_refund:
        subject, body = _refund_reply_by_status(state.last_refund_status)
        return {
            "action": "send_reply",
            "subject": subject,
            "body": body,
        }
    return {
        "action": "finish",
        "summary": _FINISH_RESOLVED_SUMMARY,
    }


def _order_id_to_link(orders: tuple[dict[str, Any], ...]) -> str:
    for order in orders:
        if order["status"] == "delivered_damaged":
            return order["id"]
    return orders[0]["id"]


def _linked_order_total(state: Any) -> Decimal | None:
    if state.linked_order_id is None:
        return None
    for order in state.orders:
        if order["id"] == state.linked_order_id:
            return Decimal(order["total"])
    return None


def _refund_reply_by_status(refund_status: str) -> tuple[str, str]:
    try:
        return _REFUND_REPLY_BY_STATUS[refund_status]
    except KeyError as missing_status:
        message = f"no reply template for refund status {refund_status!r}"
        raise ValueError(message) from missing_status


def _format_decimal_amount(amount: Decimal) -> str:
    return format(amount, "f")

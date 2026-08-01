"""Serialize prompts for the out-of-process model worker protocol."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from saferefund.agent.prompt import AgentState, OrderView, Prompt, UntrustedField
from saferefund.domain.enums import OrderStatus, RefundStatus


def serialize_prompt(prompt: Prompt) -> bytes:
    """Return UTF-8 JSON bytes for a worker request."""
    payload = {
        "text": prompt.text,
        "state": _serialize_agent_state(prompt.state),
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _serialize_agent_state(state: AgentState) -> dict[str, Any]:
    return {
        "verified": state.verified,
        "orders": [_serialize_order_view(order) for order in state.orders],
        "orders_listed": state.orders_listed,
        "linked_order_id": state.linked_order_id,
        "last_refund_status": (
            state.last_refund_status.value if state.last_refund_status else None
        ),
        "reply_sent_after_last_refund": state.reply_sent_after_last_refund,
        "menu": list(state.menu),
    }


def _serialize_order_view(order: OrderView) -> dict[str, Any]:
    return {
        "id": order.id,
        "item": {
            "provenance": order.item.provenance,
            "value": order.item.value,
        },
        "total": format(order.total, "f"),
        "status": order.status.value,
    }


def deserialize_prompt_bytes(payload: bytes) -> Prompt:
    """Rebuild a prompt from worker-protocol JSON bytes."""
    document = json.loads(payload.decode("utf-8"))
    state_document = document["state"]
    orders = tuple(
        OrderView(
            id=order["id"],
            item=UntrustedField(
                provenance="untrusted",
                value=order["item"]["value"],
            ),
            total=Decimal(order["total"]),
            status=OrderStatus(order["status"]),
        )
        for order in state_document["orders"]
    )
    last_refund_status = state_document["last_refund_status"]
    state = AgentState(
        verified=state_document["verified"],
        orders=orders,
        orders_listed=state_document["orders_listed"],
        linked_order_id=state_document["linked_order_id"],
        last_refund_status=(
            RefundStatus(last_refund_status) if last_refund_status else None
        ),
        reply_sent_after_last_refund=state_document["reply_sent_after_last_refund"],
        menu=tuple(state_document["menu"]),
    )
    return Prompt(text=document["text"], state=state)

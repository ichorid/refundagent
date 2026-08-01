"""Minimal prompt decoding for the isolated worker."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PromptStateData:
    """Structured prompt state consumed by the worker heuristic."""

    verified: bool
    orders: tuple[dict[str, Any], ...]
    orders_listed: bool
    linked_order_id: str | None
    last_refund_status: str | None
    reply_sent_after_last_refund: bool
    menu: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PromptData:
    """Prompt payload received by the worker."""

    text: str
    state: PromptStateData


def decode_prompt(prompt_bytes: bytes) -> PromptData:
    """Decode worker-protocol JSON bytes into prompt data."""
    document = json.loads(prompt_bytes.decode("utf-8"))
    state_document = document["state"]
    state = PromptStateData(
        verified=state_document["verified"],
        orders=tuple(state_document["orders"]),
        orders_listed=state_document["orders_listed"],
        linked_order_id=state_document["linked_order_id"],
        last_refund_status=state_document["last_refund_status"],
        reply_sent_after_last_refund=state_document["reply_sent_after_last_refund"],
        menu=tuple(state_document["menu"]),
    )
    return PromptData(text=document["text"], state=state)

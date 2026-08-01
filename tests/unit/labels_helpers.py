"""Static trusted classification of action types (test and documentation invariants)."""

from __future__ import annotations

from enum import StrEnum

from saferefund.actions.models import (
    ACTION_MODEL_CLASSES,
    Escalate,
    Finish,
    GetOrders,
    LinkOrder,
    ProposeRefund,
    RequestVerification,
    SendReply,
    _ActionBase,
)


class Label(StrEnum):
    """Policy vocabulary keyed by action type, never by model arguments."""

    READS_PII = "reads_pii"
    MOVES_MONEY = "moves_money"
    EXTERNAL_COMM = "external_comm"
    INTERNAL_SIDE_EFFECT = "internal_side_effect"
    TERMINAL = "terminal"


ACTION_LABELS: dict[type[_ActionBase], frozenset[Label]] = {
    GetOrders: frozenset({Label.READS_PII}),
    LinkOrder: frozenset({Label.READS_PII}),
    ProposeRefund: frozenset({Label.MOVES_MONEY}),
    SendReply: frozenset({Label.EXTERNAL_COMM}),
    RequestVerification: frozenset({Label.EXTERNAL_COMM}),
    Escalate: frozenset({Label.INTERNAL_SIDE_EFFECT}),
    Finish: frozenset({Label.TERMINAL}),
}


def labels_for_action(action: _ActionBase) -> frozenset[Label]:
    """Return the static label set for a parsed action instance."""
    return ACTION_LABELS[type(action)]


def has_label(action: _ActionBase, label: Label) -> bool:
    """Return whether the action type carries the given policy label."""
    return label in labels_for_action(action)


def all_action_types_have_label_mapping() -> bool:
    """Return whether every action model class has an explicit label entry."""
    return set(ACTION_LABELS) == set(ACTION_MODEL_CLASSES)


__all__ = [
    "ACTION_LABELS",
    "Label",
    "all_action_types_have_label_mapping",
    "has_label",
    "labels_for_action",
]

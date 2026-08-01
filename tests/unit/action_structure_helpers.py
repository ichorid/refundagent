"""Static structural guards for the action capability surface (test invariants)."""

from __future__ import annotations

import re
from typing import Final

from saferefund.actions.models import ACTION_MODEL_CLASSES, ProposeRefund

FORBIDDEN_IDENTITY_FIELD_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"customer", re.IGNORECASE),
    re.compile(r"recipient", re.IGNORECASE),
    re.compile(r"^email$", re.IGNORECASE),
    re.compile(r"envelope", re.IGNORECASE),
    re.compile(r"^to$", re.IGNORECASE),
)


def iter_action_field_names() -> tuple[str, ...]:
    """Return every field name declared on an action model."""
    field_names: list[str] = []
    for action_model in ACTION_MODEL_CLASSES:
        field_names.extend(action_model.model_fields)
    return tuple(field_names)


def forbidden_identity_field_names() -> tuple[str, ...]:
    """Return action field names that match customer or recipient identity patterns."""
    return tuple(
        field_name
        for field_name in iter_action_field_names()
        if any(
            pattern.search(field_name) for pattern in FORBIDDEN_IDENTITY_FIELD_PATTERNS
        )
    )


def propose_refund_has_amount_only() -> bool:
    """Return whether refund proposals expose only the amount claim."""
    return set(ProposeRefund.model_fields) == {"action", "amount"}


def all_action_models_forbid_extra_fields() -> bool:
    """Return whether every action model rejects unknown JSON keys."""
    return all(
        action_model.model_config.get("extra") == "forbid"
        for action_model in ACTION_MODEL_CLASSES
    )

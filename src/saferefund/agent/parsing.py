"""Parse untrusted model text into typed actions without raising."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from pydantic import TypeAdapter, ValidationError

from saferefund.actions.models import Action, ProposeRefund

if TYPE_CHECKING:
    from decimal import Decimal

_MAX_REFUND_DECIMAL_PLACES: Final = 2
_ACTION_ADAPTER: TypeAdapter[Action] = TypeAdapter(Action)


@dataclass(frozen=True, slots=True)
class ParseSuccess:
    """A model output that parsed into a single legal action."""

    action: Action


@dataclass(frozen=True, slots=True)
class ParseFailure:
    """A model output that could not be turned into an action."""

    error: str


def parse(raw_model_output: str) -> ParseSuccess | ParseFailure:
    """Turn validated model text into an explicit parse outcome.

    Total for ``str``: always returns ``ParseSuccess`` or ``ParseFailure``.
    Runtime type checking belongs at the model gateway boundary.
    """
    stripped_output = raw_model_output.strip()
    if not stripped_output:
        return ParseFailure(error="empty model output")

    try:
        decoded_payload = json.loads(stripped_output)
    except json.JSONDecodeError as decode_error:
        return ParseFailure(error=f"malformed JSON: {decode_error.msg}")

    if not isinstance(decoded_payload, dict):
        return ParseFailure(error="model output must be a JSON object")

    try:
        parsed_action = _ACTION_ADAPTER.validate_python(decoded_payload)
    except ValidationError as validation_error:
        return ParseFailure(error=_format_validation_error(validation_error))

    if isinstance(parsed_action, ProposeRefund):
        decimal_error = _propose_refund_decimal_error(parsed_action.amount)
        if decimal_error is not None:
            return ParseFailure(error=decimal_error)

    return ParseSuccess(action=parsed_action)


def _format_validation_error(validation_error: ValidationError) -> str:
    first_error = validation_error.errors()[0]
    location = _format_error_location(first_error.get("loc", ()))
    message = first_error.get("msg", "invalid action")
    if location:
        return f"{location}: {message}"
    return str(message)


def _format_error_location(location: tuple[Any, ...]) -> str:
    if not location:
        return ""
    return ".".join(str(segment) for segment in location)


def _propose_refund_decimal_error(amount: Decimal) -> str | None:
    if not amount.is_finite():
        return "propose_refund amount must be finite"
    exponent = amount.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -_MAX_REFUND_DECIMAL_PLACES:
        return "propose_refund amount must have at most two decimal places"
    return None


__all__ = ["ParseFailure", "ParseSuccess", "parse"]

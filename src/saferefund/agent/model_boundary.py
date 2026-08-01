"""Model gateway invocation, runtime validation, and parsing boundary."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from saferefund import config
from saferefund.agent.parsing import ParseFailure, ParseSuccess, parse
from saferefund.domain.enums import EscalationOrigin
from saferefund.gate.operations import escalate_case

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from saferefund.agent.gateway import ModelGateway
    from saferefund.agent.prompt import Prompt

_MODEL_FAILURE_REASON_PREFIX = "The model call failed with an exception of type"


class ModelResponseTypeError(TypeError):
    """Raised when the gateway returns a value that is not an exact str."""


@dataclass(frozen=True, slots=True)
class ModelBoundaryParse:
    """Validated gateway text and its parse outcome."""

    raw_model_output: str
    outcome: ParseSuccess | ParseFailure


def _model_failure_reason(model_failure: Exception) -> str:
    """Return a fixed escalation reason that carries no untrusted model text."""
    return f"{_MODEL_FAILURE_REASON_PREFIX} {type(model_failure).__name__}."


def _require_exact_str(runtime_value: object) -> str:
    """Reject non-string and hostile str subclasses before parsing."""
    if type(runtime_value) is not str:
        message = "model response must be an exact str at runtime"
        raise ModelResponseTypeError(message)
    return runtime_value


async def invoke_model_boundary(
    session: AsyncSession,
    *,
    case_id: str,
    model_gateway: ModelGateway,
    prompt: Prompt,
) -> ModelBoundaryParse | None:
    """Invoke the gateway, validate the runtime result, parse, or close on failure.

    Returns ``None`` when the case is closed after ``model_failure`` escalation.
    Malformed but syntactically valid strings remain ``ParseFailure`` outcomes.
    """
    try:
        async with asyncio.timeout(config.MODEL_CALL_TIMEOUT_SECONDS):
            runtime_value = await model_gateway.propose(prompt)
            validated_output = _require_exact_str(runtime_value)
            return ModelBoundaryParse(
                raw_model_output=validated_output,
                outcome=parse(validated_output),
            )
    except Exception as model_failure:  # noqa: BLE001 — untrusted dependency boundary
        await escalate_case(
            session,
            case_id,
            origin=EscalationOrigin.MODEL_FAILURE,
            reason=_model_failure_reason(model_failure),
        )
        return None


__all__ = [
    "ModelBoundaryParse",
    "ModelResponseTypeError",
    "invoke_model_boundary",
]

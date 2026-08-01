"""Shared exact event-type sequence assertions for lifecycle proof tests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from saferefund import config
from saferefund.adapters import mailer, payment, ticketing
from saferefund.domain.enums import RefundStatus
from saferefund.domain.events import EventType
from saferefund.domain.payloads import (
    CaseClosedPayload,
    EscalatedPayload,
    RefundApprovalRequiredPayload,
    RefundApprovedPayload,
    RefundExecutedPayload,
    RefundExpiredPayload,
    RefundProposedPayload,
    RefundRejectedPayload,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from decimal import Decimal

    from saferefund.domain.enums import CaseOutcome, EscalationOrigin
    from saferefund.domain.tables import RefundRow

MODEL_FAILURE_REASON_PREFIX = "The model call failed with an exception of type"

INBOUND_EMAIL_OPENING_SEQUENCE: tuple[EventType, ...] = (
    EventType.CASE_OPENED,
    EventType.EMAIL_RECEIVED,
)

TERMINAL_ESCALATION_CLOSURE_SUFFIX: tuple[EventType, ...] = (
    EventType.ESCALATED,
    EventType.CASE_CLOSED,
)

GATE_PENDING_APPROVAL_SEQUENCE: tuple[EventType, ...] = (
    EventType.CASE_OPENED,
    EventType.ORDERS_LISTED,
    EventType.ORDER_LINKED,
    EventType.REFUND_PROPOSED,
    EventType.REFUND_APPROVAL_REQUIRED,
)

INBOUND_PENDING_APPROVAL_SEQUENCE: tuple[EventType, ...] = (
    EventType.CASE_OPENED,
    EventType.EMAIL_RECEIVED,
    EventType.ORDERS_LISTED,
    EventType.ORDER_LINKED,
    EventType.REFUND_PROPOSED,
    EventType.REFUND_APPROVAL_REQUIRED,
)

EXPIRY_WITH_AGENT_RESUME_SEQUENCE: tuple[EventType, ...] = (
    EventType.REFUND_EXPIRED,
    EventType.CASE_CLOSED,
)

EXPIRY_AGENT_RESUME_SUFFIX: tuple[EventType, ...] = (EventType.CASE_CLOSED,)

OPERATOR_APPROVE_RESUME_SUFFIX: tuple[EventType, ...] = (
    EventType.REFUND_APPROVED,
    EventType.REFUND_EXECUTED,
    EventType.REPLY_SENT,
    EventType.CASE_CLOSED,
)

OPERATOR_REJECT_RESUME_SUFFIX: tuple[EventType, ...] = (
    EventType.REFUND_REJECTED,
    EventType.REPLY_SENT,
    EventType.CASE_CLOSED,
)

OPERATOR_APPROVE_CONFLICT_AFTER_REJECT_SEQUENCE: tuple[EventType, ...] = (
    *GATE_PENDING_APPROVAL_SEQUENCE,
    EventType.REFUND_REJECTED,
)

OPERATOR_REJECT_CONFLICT_AFTER_APPROVE_SEQUENCE: tuple[EventType, ...] = (
    *GATE_PENDING_APPROVAL_SEQUENCE,
    EventType.REFUND_APPROVED,
    EventType.REFUND_EXECUTED,
)


def denial_loop_termination_sequence(
    *,
    denial_count: int = config.DENIAL_LOOP_THRESHOLD,
) -> tuple[EventType, ...]:
    """Return the inbound denial-loop path ending in policy escalation."""
    return (
        *INBOUND_EMAIL_OPENING_SEQUENCE,
        *(EventType.ACTION_DENIED,) * denial_count,
        *TERMINAL_ESCALATION_CLOSURE_SUFFIX,
    )


def agent_escalation_termination_sequence() -> tuple[EventType, ...]:
    """Return the inbound agent-escalation path ending in immediate closure."""
    return (
        *INBOUND_EMAIL_OPENING_SEQUENCE,
        *TERMINAL_ESCALATION_CLOSURE_SUFFIX,
    )


def step_limit_termination_sequence(
    *,
    allowed_steps: int = config.MAX_AGENT_STEPS,
) -> tuple[EventType, ...]:
    """Return the inbound step-limit path with the configured allowed outcomes."""
    return (
        *INBOUND_EMAIL_OPENING_SEQUENCE,
        *(EventType.ORDERS_LISTED,) * allowed_steps,
        *TERMINAL_ESCALATION_CLOSURE_SUFFIX,
    )


def parse_limit_termination_sequence(
    *,
    invalid_count: int = config.MAX_INVALID_OUTPUTS,
) -> tuple[EventType, ...]:
    """Return the inbound parse-limit path with the configured invalid events."""
    return (
        *INBOUND_EMAIL_OPENING_SEQUENCE,
        *(EventType.INVALID_OUTPUT,) * invalid_count,
        *TERMINAL_ESCALATION_CLOSURE_SUFFIX,
    )


def model_failure_termination_sequence() -> tuple[EventType, ...]:
    """Return the inbound model-failure path ending in immediate closure."""
    return agent_escalation_termination_sequence()


def operator_approve_success_sequence() -> tuple[EventType, ...]:
    """Return the gate-pending path completed by operator approval and agent resume."""
    return (*GATE_PENDING_APPROVAL_SEQUENCE, *OPERATOR_APPROVE_RESUME_SUFFIX)


def operator_reject_success_sequence() -> tuple[EventType, ...]:
    """Return the gate-pending path completed by operator rejection and agent resume."""
    return (*GATE_PENDING_APPROVAL_SEQUENCE, *OPERATOR_REJECT_RESUME_SUFFIX)


def event_types(case_events: Sequence[Any]) -> list[EventType]:
    """Return the ordered event types of a loaded case or customer stream."""
    return [event.event_type for event in case_events]


def assert_exact_event_type_sequence(
    case_events: Sequence[Any],
    expected_types: Sequence[EventType],
) -> None:
    """Fail unless the stream's event types match exactly in order and length."""
    actual_types = event_types(case_events)
    expected = list(expected_types)
    assert actual_types == expected, (
        "event-type sequence mismatch\n"
        f"  expected ({len(expected)}): {expected!r}\n"
        f"  actual   ({len(actual_types)}): {actual_types!r}"
    )


def assert_expired_refund_id_matches_lifecycle(
    case_events: Sequence[Any],
    *,
    refund_id: str,
) -> None:
    """Bind refund_expired to the proposed and approval-required refund ids."""
    proposed_event = next(
        event for event in case_events if event.event_type is EventType.REFUND_PROPOSED
    )
    approval_required_event = next(
        event
        for event in case_events
        if event.event_type is EventType.REFUND_APPROVAL_REQUIRED
    )
    expired_event = next(
        event for event in case_events if event.event_type is EventType.REFUND_EXPIRED
    )

    proposed_payload = RefundProposedPayload.model_validate(proposed_event.payload)
    approval_required_payload = RefundApprovalRequiredPayload.model_validate(
        approval_required_event.payload,
    )
    expired_payload = RefundExpiredPayload.model_validate(expired_event.payload)

    assert proposed_payload.refund_id == refund_id
    assert approval_required_payload.refund_id == refund_id
    assert expired_payload.refund_id == refund_id


def assert_refund_expired_followed_by_resume(
    case_events: Sequence[Any],
    *,
    resumed_types: Sequence[EventType] = EXPIRY_AGENT_RESUME_SUFFIX,
) -> None:
    """Assert refund_expired is immediately followed by the resumed agent sequence."""
    actual_types = event_types(case_events)
    expired_index = actual_types.index(EventType.REFUND_EXPIRED)
    resumed = list(resumed_types)
    assert actual_types[expired_index + 1 : expired_index + 1 + len(resumed)] == resumed
    assert expired_index + 1 + len(resumed) == len(actual_types), (
        "events after the resumed-agent sequence are not allowed: "
        f"{actual_types[expired_index + 1 + len(resumed) :]!r}"
    )


def assert_case_expired_with_agent_resume(
    case_events: Sequence[Any],
    *,
    pending_sequence: Sequence[EventType],
    refund_id: str,
) -> None:
    """Assert the full expiry conflict path and lifecycle refund identity joins."""
    assert_exact_event_type_sequence(
        case_events,
        [*pending_sequence, *EXPIRY_WITH_AGENT_RESUME_SEQUENCE],
    )
    assert_expired_refund_id_matches_lifecycle(case_events, refund_id=refund_id)
    assert_refund_expired_followed_by_resume(case_events)


def assert_escalation_only_adapter_effects() -> None:
    """Assert termination side effects are exactly one ticketing escalation."""
    assert mailer.outbox == []
    assert payment.calls == []
    assert len(ticketing.escalations) == 1


def assert_operator_adapter_effects(
    *,
    payment_calls: Sequence[payment.RefundCall],
    mailer_messages: Sequence[mailer.OutboxMessage] | None = None,
) -> None:
    """Assert operator HTTP adapter side effects are exact and exclude ticketing."""
    assert payment.calls == list(payment_calls)
    if mailer_messages is not None:
        assert mailer.outbox == list(mailer_messages)
    assert ticketing.escalations == []


def assert_refund_row_matches_operator_outcome(
    refund_row: RefundRow,
    *,
    refund_id: str,
    amount: Decimal,
    order_id: str,
    status: RefundStatus,
) -> None:
    """Bind the refund row to the operator lifecycle identifiers and amount."""
    assert refund_row.id == refund_id
    assert refund_row.amount == amount
    assert refund_row.order_id == order_id
    assert refund_row.status is status


def _operator_lifecycle_refund_events(
    case_events: Sequence[Any],
) -> tuple[Any, Any, Any | None, Any | None]:
    proposed_event = next(
        event for event in case_events if event.event_type is EventType.REFUND_PROPOSED
    )
    approval_required_event = next(
        event
        for event in case_events
        if event.event_type is EventType.REFUND_APPROVAL_REQUIRED
    )
    approved_event = next(
        (
            event
            for event in case_events
            if event.event_type is EventType.REFUND_APPROVED
        ),
        None,
    )
    executed_event = next(
        (
            event
            for event in case_events
            if event.event_type is EventType.REFUND_EXECUTED
        ),
        None,
    )
    return proposed_event, approval_required_event, approved_event, executed_event


def assert_operator_approve_response_lifecycle(  # noqa: PLR0913
    case_events: Sequence[Any],
    *,
    refund_id: str,
    amount: Decimal,
    operator_id: str,
    order_id: str,
    mailer_messages: Sequence[mailer.OutboxMessage],
    refund_status: RefundStatus = RefundStatus.EXECUTED,
    refund_row: RefundRow | None = None,
) -> None:
    """Assert operator approval HTTP evidence: sequence, identity, payment, and row."""
    assert_exact_event_type_sequence(case_events, operator_approve_success_sequence())

    proposed_event, approval_required_event, approved_event, executed_event = (
        _operator_lifecycle_refund_events(case_events)
    )
    assert approved_event is not None
    assert executed_event is not None

    proposed_payload = RefundProposedPayload.model_validate(proposed_event.payload)
    approval_required_payload = RefundApprovalRequiredPayload.model_validate(
        approval_required_event.payload,
    )
    approved_payload = RefundApprovedPayload.model_validate(approved_event.payload)
    executed_payload = RefundExecutedPayload.model_validate(executed_event.payload)

    assert proposed_payload.refund_id == refund_id
    assert approval_required_payload.refund_id == refund_id
    assert approved_payload.refund_id == refund_id
    assert executed_payload.refund_id == refund_id
    assert proposed_payload.amount == amount
    assert approval_required_payload.amount == amount
    assert executed_payload.amount == amount
    assert approved_payload.operator_id == operator_id
    assert executed_payload.provider_ref == f"pay_{refund_id}"

    for event in (
        proposed_event,
        approval_required_event,
        approved_event,
        executed_event,
    ):
        assert event.order_id == order_id

    expected_payment_call = payment.RefundCall(
        idempotency_key=refund_id,
        amount=amount,
    )
    assert_operator_adapter_effects(
        payment_calls=[expected_payment_call],
        mailer_messages=mailer_messages,
    )

    if refund_row is not None:
        assert_refund_row_matches_operator_outcome(
            refund_row,
            refund_id=refund_id,
            amount=amount,
            order_id=order_id,
            status=refund_status,
        )


def assert_operator_reject_response_lifecycle(  # noqa: PLR0913
    case_events: Sequence[Any],
    *,
    refund_id: str,
    amount: Decimal,
    operator_id: str,
    order_id: str,
    reason: str,
    mailer_messages: Sequence[mailer.OutboxMessage],
    refund_status: RefundStatus = RefundStatus.REJECTED,
    refund_row: RefundRow | None = None,
) -> None:
    """Assert operator rejection HTTP evidence: sequence, identity, and no payment."""
    assert_exact_event_type_sequence(case_events, operator_reject_success_sequence())

    proposed_event, approval_required_event, approved_event, executed_event = (
        _operator_lifecycle_refund_events(case_events)
    )
    assert approved_event is None
    assert executed_event is None

    proposed_payload = RefundProposedPayload.model_validate(proposed_event.payload)
    approval_required_payload = RefundApprovalRequiredPayload.model_validate(
        approval_required_event.payload,
    )
    rejected_event = next(
        event for event in case_events if event.event_type is EventType.REFUND_REJECTED
    )
    rejected_payload = RefundRejectedPayload.model_validate(rejected_event.payload)

    assert proposed_payload.refund_id == refund_id
    assert approval_required_payload.refund_id == refund_id
    assert rejected_payload.refund_id == refund_id
    assert proposed_payload.amount == amount
    assert approval_required_payload.amount == amount
    assert rejected_payload.operator_id == operator_id
    assert rejected_payload.reason == reason

    for event in (proposed_event, approval_required_event, rejected_event):
        assert event.order_id == order_id

    assert_operator_adapter_effects(
        payment_calls=[],
        mailer_messages=mailer_messages,
    )

    if refund_row is not None:
        assert_refund_row_matches_operator_outcome(
            refund_row,
            refund_id=refund_id,
            amount=amount,
            order_id=order_id,
            status=refund_status,
        )


def assert_operator_conflict_response_unchanged(
    case_events: Sequence[Any],
    *,
    expected_types: Sequence[EventType],
    payment_calls: Sequence[payment.RefundCall],
) -> None:
    """Assert conflict responses add no approval, rejection, payment, or resume."""
    assert_exact_event_type_sequence(case_events, expected_types)
    assert_operator_adapter_effects(
        payment_calls=payment_calls,
        mailer_messages=[],
    )


def assert_terminal_escalation_closure(  # noqa: PLR0913
    case_events: Sequence[Any],
    *,
    expected_types: Sequence[EventType],
    origin: EscalationOrigin,
    outcome: CaseOutcome,
    reason_prefix: str | None = None,
    reason_suffix: str | None = None,
) -> str:
    """Assert the full termination sequence, payloads, and ticket binding."""
    assert_exact_event_type_sequence(case_events, expected_types)

    escalated = case_events[-2]
    closed = case_events[-1]
    assert escalated.event_type is EventType.ESCALATED
    assert closed.event_type is EventType.CASE_CLOSED

    escalated_payload = EscalatedPayload.model_validate(escalated.payload)
    closed_payload = CaseClosedPayload.model_validate(closed.payload)
    assert escalated_payload.origin is origin
    assert closed_payload.outcome is outcome
    if reason_prefix is not None:
        assert escalated_payload.reason.startswith(reason_prefix)
        assert escalated_payload.reason.endswith(".")
        assert "{" not in escalated_payload.reason
        assert "}" not in escalated_payload.reason
    if reason_suffix is not None:
        assert escalated_payload.reason.endswith(reason_suffix)

    assert_escalation_only_adapter_effects()
    assert escalated_payload.ticket_id == ticketing.escalations[0].ticket_id
    return escalated_payload.ticket_id

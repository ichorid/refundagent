"""Case-scoped control-state projection."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from saferefund.domain.enums import (
    CaseOutcome,
    CaseStatus,
    EscalationOrigin,
    RefundStatus,
)
from saferefund.domain.events import EventType, validate_payload
from saferefund.domain.payloads import (
    CaseClosedPayload,
    EscalatedPayload,
    RefundApprovalRequiredPayload,
    VerificationRequestedPayload,
)
from saferefund.projections.types import FoldableEvent

_REFUND_LIFECYCLE_EVENT_TYPES: Final[frozenset[EventType]] = frozenset(
    {
        EventType.REFUND_PROPOSED,
        EventType.REFUND_AUTO_APPROVED,
        EventType.REFUND_APPROVAL_REQUIRED,
        EventType.REFUND_APPROVED,
        EventType.REFUND_REJECTED,
        EventType.REFUND_EXPIRED,
        EventType.REFUND_EXECUTED,
    }
)
_AGENT_ATTEMPT_OUTCOME_TYPES: Final[frozenset[EventType]] = frozenset(
    {
        EventType.ORDERS_LISTED,
        EventType.ORDER_LINKED,
        EventType.VERIFICATION_REQUESTED,
        EventType.REPLY_SENT,
        EventType.REFUND_PROPOSED,
        EventType.ACTION_DENIED,
        EventType.INVALID_OUTPUT,
    }
)
_SUCCESSFUL_PARSE_OUTCOME_TYPES: Final[frozenset[EventType]] = frozenset(
    {
        EventType.ORDERS_LISTED,
        EventType.ORDER_LINKED,
        EventType.VERIFICATION_REQUESTED,
        EventType.REPLY_SENT,
        EventType.REFUND_PROPOSED,
    }
)
_AGENT_STEP_ESCALATION_ORIGINS: Final[frozenset[EscalationOrigin]] = frozenset(
    {
        EscalationOrigin.AGENT,
        EscalationOrigin.POLICY,
    }
)


@dataclass(frozen=True, slots=True)
class CaseSummary:
    """Folded per-case control state used by policy and the agent loop."""

    case_id: str
    customer_id: str
    status: CaseStatus
    linked_order_id: str | None
    orders_listed: bool
    step_count: int
    consecutive_denials: int
    consecutive_invalid_outputs: int
    last_refund_status: RefundStatus | None
    reply_sent_after_last_refund: bool


@dataclass
class _CaseFoldState:
    has_case_closed: bool = False
    linked_order_id: str | None = None
    orders_listed: bool = False
    step_count: int = 0
    consecutive_denials: int = 0
    consecutive_invalid_outputs: int = 0
    pending_refund_id: str | None = None
    last_refund_status: RefundStatus | None = None
    last_refund_lifecycle_seq: int | None = None
    reply_sent_after_last_refund: bool = False
    verification_token: str | None = None
    verification_expires_at: datetime | None = None
    customer_verified: bool = False


def _case_fold_events(
    case_id: str,
    customer_id: str,
    events: Sequence[FoldableEvent],
) -> list[FoldableEvent]:
    scoped_events: list[FoldableEvent] = []
    for event in events:
        if event.customer_id != customer_id:
            continue
        if event.case_id == case_id:
            scoped_events.append(event)
            continue
        if event.event_type is EventType.CUSTOMER_VERIFIED:
            scoped_events.append(event)
    return sorted(scoped_events, key=lambda fold_event: fold_event.seq)


def _record_refund_lifecycle(
    fold_state: _CaseFoldState,
    *,
    refund_status: RefundStatus,
    event_seq: int,
) -> None:
    fold_state.last_refund_status = refund_status
    fold_state.last_refund_lifecycle_seq = event_seq
    fold_state.reply_sent_after_last_refund = False


def _escalation_origin(event: FoldableEvent) -> EscalationOrigin:
    escalated = EscalatedPayload.model_validate(
        validate_payload(event.event_type, dict(event.payload)).model_dump()
    )
    return escalated.origin


def _case_closed_outcome(event: FoldableEvent) -> CaseOutcome:
    case_closed = CaseClosedPayload.model_validate(
        validate_payload(event.event_type, dict(event.payload)).model_dump()
    )
    return case_closed.outcome


def _event_counts_as_agent_step(event: FoldableEvent) -> bool:
    if event.event_type in _AGENT_ATTEMPT_OUTCOME_TYPES:
        return True
    if event.event_type is EventType.ESCALATED:
        return _escalation_origin(event) in _AGENT_STEP_ESCALATION_ORIGINS
    if event.event_type is EventType.CASE_CLOSED:
        return _case_closed_outcome(event) is CaseOutcome.FINISHED
    return False


def _event_is_successful_parse_outcome(event: FoldableEvent) -> bool:
    if event.event_type in _SUCCESSFUL_PARSE_OUTCOME_TYPES:
        return True
    if event.event_type is EventType.ESCALATED:
        return _escalation_origin(event) is EscalationOrigin.AGENT
    if event.event_type is EventType.CASE_CLOSED:
        return _case_closed_outcome(event) is CaseOutcome.FINISHED
    return False


def _apply_agent_attempt_accounting(
    fold_state: _CaseFoldState,
    event: FoldableEvent,
) -> None:
    if _event_counts_as_agent_step(event):
        fold_state.step_count += 1
    if _event_is_successful_parse_outcome(event):
        fold_state.consecutive_denials = 0
        fold_state.consecutive_invalid_outputs = 0
    elif event.event_type is EventType.ACTION_DENIED:
        fold_state.consecutive_denials += 1
        fold_state.consecutive_invalid_outputs = 0
    elif event.event_type is EventType.INVALID_OUTPUT:
        fold_state.consecutive_invalid_outputs += 1


def _apply_refund_event(
    fold_state: _CaseFoldState,
    event: FoldableEvent,
) -> None:
    match event.event_type:
        case EventType.REFUND_PROPOSED:
            validate_payload(event.event_type, dict(event.payload))
            _record_refund_lifecycle(
                fold_state,
                refund_status=RefundStatus.PENDING_APPROVAL,
                event_seq=event.seq,
            )
        case EventType.REFUND_AUTO_APPROVED:
            validate_payload(event.event_type, dict(event.payload))
            _record_refund_lifecycle(
                fold_state,
                refund_status=RefundStatus.APPROVED,
                event_seq=event.seq,
            )
        case EventType.REFUND_APPROVAL_REQUIRED:
            approval_required = RefundApprovalRequiredPayload.model_validate(
                validate_payload(event.event_type, dict(event.payload)).model_dump()
            )
            fold_state.pending_refund_id = approval_required.refund_id
            _record_refund_lifecycle(
                fold_state,
                refund_status=RefundStatus.PENDING_APPROVAL,
                event_seq=event.seq,
            )
        case EventType.REFUND_APPROVED:
            validate_payload(event.event_type, dict(event.payload))
            fold_state.pending_refund_id = None
            _record_refund_lifecycle(
                fold_state,
                refund_status=RefundStatus.APPROVED,
                event_seq=event.seq,
            )
        case EventType.REFUND_REJECTED:
            validate_payload(event.event_type, dict(event.payload))
            fold_state.pending_refund_id = None
            _record_refund_lifecycle(
                fold_state,
                refund_status=RefundStatus.REJECTED,
                event_seq=event.seq,
            )
        case EventType.REFUND_EXPIRED:
            validate_payload(event.event_type, dict(event.payload))
            fold_state.pending_refund_id = None
            _record_refund_lifecycle(
                fold_state,
                refund_status=RefundStatus.EXPIRED,
                event_seq=event.seq,
            )
        case EventType.REFUND_EXECUTED:
            validate_payload(event.event_type, dict(event.payload))
            _record_refund_lifecycle(
                fold_state,
                refund_status=RefundStatus.EXECUTED,
                event_seq=event.seq,
            )
        case _:
            return


def _apply_reply_sent(fold_state: _CaseFoldState, event: FoldableEvent) -> None:
    _apply_agent_attempt_accounting(fold_state, event)
    if (
        fold_state.last_refund_lifecycle_seq is not None
        and event.seq > fold_state.last_refund_lifecycle_seq
    ):
        fold_state.reply_sent_after_last_refund = True


def _apply_case_event(fold_state: _CaseFoldState, event: FoldableEvent) -> None:
    match event.event_type:
        case EventType.CUSTOMER_VERIFIED:
            fold_state.customer_verified = True
        case EventType.CASE_CLOSED:
            _apply_agent_attempt_accounting(fold_state, event)
            fold_state.has_case_closed = True
        case EventType.ORDERS_LISTED:
            _apply_agent_attempt_accounting(fold_state, event)
            fold_state.orders_listed = True
        case EventType.ORDER_LINKED:
            _apply_agent_attempt_accounting(fold_state, event)
            fold_state.linked_order_id = event.order_id
        case EventType.VERIFICATION_REQUESTED:
            _apply_agent_attempt_accounting(fold_state, event)
            verification_requested = VerificationRequestedPayload.model_validate(
                validate_payload(event.event_type, dict(event.payload)).model_dump()
            )
            fold_state.verification_token = verification_requested.token
            fold_state.verification_expires_at = verification_requested.expires_at
        case EventType.ACTION_DENIED | EventType.INVALID_OUTPUT | EventType.ESCALATED:
            _apply_agent_attempt_accounting(fold_state, event)
        case event_type if event_type in _REFUND_LIFECYCLE_EVENT_TYPES:
            if event_type is EventType.REFUND_PROPOSED:
                _apply_agent_attempt_accounting(fold_state, event)
            _apply_refund_event(fold_state, event)
        case EventType.REPLY_SENT:
            _apply_reply_sent(fold_state, event)


def _resolve_case_status(
    fold_state: _CaseFoldState,
    now: datetime,
) -> CaseStatus:
    if fold_state.has_case_closed:
        return CaseStatus.CLOSED
    if fold_state.pending_refund_id is not None:
        return CaseStatus.AWAITING_APPROVAL
    if (
        fold_state.verification_token is not None
        and not fold_state.customer_verified
        and fold_state.verification_expires_at is not None
        and now < fold_state.verification_expires_at
    ):
        return CaseStatus.AWAITING_VERIFICATION
    return CaseStatus.OPEN


def project_case_summary(
    *,
    case_id: str,
    customer_id: str,
    events: Sequence[FoldableEvent],
    now: datetime,
) -> CaseSummary:
    """Fold case-scoped events plus customer verification for status evaluation."""
    fold_state = _CaseFoldState()
    for event in _case_fold_events(case_id, customer_id, events):
        _apply_case_event(fold_state, event)

    return CaseSummary(
        case_id=case_id,
        customer_id=customer_id,
        status=_resolve_case_status(fold_state, now),
        linked_order_id=fold_state.linked_order_id,
        orders_listed=fold_state.orders_listed,
        step_count=fold_state.step_count,
        consecutive_denials=fold_state.consecutive_denials,
        consecutive_invalid_outputs=fold_state.consecutive_invalid_outputs,
        last_refund_status=fold_state.last_refund_status,
        reply_sent_after_last_refund=fold_state.reply_sent_after_last_refund,
    )

"""Canonical event construction with exhaustive catalogue validation."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel

from saferefund.domain.enums import Actor, Channel, EscalationOrigin, VerificationMethod
from saferefund.domain.payloads import (
    ActionDeniedPayload,
    CaseClosedPayload,
    CaseOpenedPayload,
    CustomerVerifiedPayload,
    EmailReceivedPayload,
    EscalatedPayload,
    InvalidOutputPayload,
    OrderLinkedPayload,
    OrdersListedPayload,
    RefundApprovalRequiredPayload,
    RefundApprovedPayload,
    RefundAutoApprovedPayload,
    RefundExecutedPayload,
    RefundExpiredPayload,
    RefundProposedPayload,
    RefundRejectedPayload,
    ReplySentPayload,
    VerificationRequestedPayload,
)

type PayloadModel = type[BaseModel]


class EventType(StrEnum):
    """Exhaustive event catalogue from the architecture."""

    CASE_OPENED = "case_opened"
    EMAIL_RECEIVED = "email_received"
    ORDERS_LISTED = "orders_listed"
    ORDER_LINKED = "order_linked"
    VERIFICATION_REQUESTED = "verification_requested"
    CUSTOMER_VERIFIED = "customer_verified"
    REFUND_PROPOSED = "refund_proposed"
    REFUND_AUTO_APPROVED = "refund_auto_approved"
    REFUND_APPROVAL_REQUIRED = "refund_approval_required"
    REFUND_APPROVED = "refund_approved"
    REFUND_REJECTED = "refund_rejected"
    REFUND_EXPIRED = "refund_expired"
    REFUND_EXECUTED = "refund_executed"
    REPLY_SENT = "reply_sent"
    ESCALATED = "escalated"
    ACTION_DENIED = "action_denied"
    INVALID_OUTPUT = "invalid_output"
    CASE_CLOSED = "case_closed"


PAYLOAD_MODEL_BY_EVENT_TYPE: Final[dict[EventType, PayloadModel]] = {
    EventType.CASE_OPENED: CaseOpenedPayload,
    EventType.EMAIL_RECEIVED: EmailReceivedPayload,
    EventType.ORDERS_LISTED: OrdersListedPayload,
    EventType.ORDER_LINKED: OrderLinkedPayload,
    EventType.VERIFICATION_REQUESTED: VerificationRequestedPayload,
    EventType.CUSTOMER_VERIFIED: CustomerVerifiedPayload,
    EventType.REFUND_PROPOSED: RefundProposedPayload,
    EventType.REFUND_AUTO_APPROVED: RefundAutoApprovedPayload,
    EventType.REFUND_APPROVAL_REQUIRED: RefundApprovalRequiredPayload,
    EventType.REFUND_APPROVED: RefundApprovedPayload,
    EventType.REFUND_REJECTED: RefundRejectedPayload,
    EventType.REFUND_EXPIRED: RefundExpiredPayload,
    EventType.REFUND_EXECUTED: RefundExecutedPayload,
    EventType.REPLY_SENT: ReplySentPayload,
    EventType.ESCALATED: EscalatedPayload,
    EventType.ACTION_DENIED: ActionDeniedPayload,
    EventType.INVALID_OUTPUT: InvalidOutputPayload,
    EventType.CASE_CLOSED: CaseClosedPayload,
}

_CASE_SCOPED_EVENT_TYPES: Final[frozenset[EventType]] = frozenset(
    {
        EventType.CASE_OPENED,
        EventType.EMAIL_RECEIVED,
        EventType.ORDERS_LISTED,
        EventType.VERIFICATION_REQUESTED,
        EventType.REPLY_SENT,
        EventType.ACTION_DENIED,
        EventType.INVALID_OUTPUT,
        EventType.ESCALATED,
        EventType.CASE_CLOSED,
    }
)

_CASE_AND_ORDER_SCOPED_EVENT_TYPES: Final[frozenset[EventType]] = frozenset(
    {
        EventType.ORDER_LINKED,
        EventType.REFUND_PROPOSED,
        EventType.REFUND_AUTO_APPROVED,
        EventType.REFUND_APPROVAL_REQUIRED,
        EventType.REFUND_APPROVED,
        EventType.REFUND_REJECTED,
        EventType.REFUND_EXPIRED,
        EventType.REFUND_EXECUTED,
    }
)

_CUSTOMER_SCOPED_EVENT_TYPES: Final[frozenset[EventType]] = frozenset(
    {EventType.CUSTOMER_VERIFIED}
)

_FIXED_ACTOR_CHANNEL_BY_EVENT_TYPE: Final[dict[EventType, tuple[Actor, Channel]]] = {
    EventType.CASE_OPENED: (Actor.SYSTEM, Channel.INTERNAL),
    EventType.EMAIL_RECEIVED: (Actor.CUSTOMER, Channel.EMAIL),
    EventType.ORDERS_LISTED: (Actor.AGENT, Channel.INTERNAL),
    EventType.ORDER_LINKED: (Actor.AGENT, Channel.INTERNAL),
    EventType.VERIFICATION_REQUESTED: (Actor.AGENT, Channel.INTERNAL),
    EventType.REFUND_PROPOSED: (Actor.AGENT, Channel.INTERNAL),
    EventType.REFUND_AUTO_APPROVED: (Actor.SYSTEM, Channel.INTERNAL),
    EventType.REFUND_APPROVAL_REQUIRED: (Actor.SYSTEM, Channel.INTERNAL),
    EventType.REFUND_APPROVED: (Actor.OPERATOR, Channel.OPERATOR_API),
    EventType.REFUND_REJECTED: (Actor.OPERATOR, Channel.OPERATOR_API),
    EventType.REFUND_EXPIRED: (Actor.SYSTEM, Channel.INTERNAL),
    EventType.REFUND_EXECUTED: (Actor.SYSTEM, Channel.INTERNAL),
    EventType.REPLY_SENT: (Actor.AGENT, Channel.INTERNAL),
    EventType.ACTION_DENIED: (Actor.SYSTEM, Channel.INTERNAL),
    EventType.INVALID_OUTPUT: (Actor.SYSTEM, Channel.INTERNAL),
    EventType.CASE_CLOSED: (Actor.SYSTEM, Channel.INTERNAL),
}


class UnknownEventTypeError(ValueError):
    """Raised when an event type is outside the catalogue."""

    def __init__(self, event_type: str) -> None:
        """Record the rejected catalogue value."""
        super().__init__(f"Unknown event type: {event_type}")


class InvalidActorChannelError(ValueError):
    """Raised when actor and channel do not match the catalogue entry."""

    def __init__(
        self,
        event_type: EventType,
        expected_actor: Actor,
        expected_channel: Channel,
        actor: Actor,
        channel: Channel,
    ) -> None:
        """Record the expected and actual actor/channel pair."""
        message = (
            f"{event_type.value} requires actor={expected_actor.value} "
            f"and channel={expected_channel.value}, "
            f"got actor={actor.value} and channel={channel.value}"
        )
        super().__init__(message)


class InvalidEventScopeError(ValueError):
    """Raised when case_id or order_id do not match the event scope."""

    def __init__(self, message: str) -> None:
        """Record the scope violation."""
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    """Validated event ready for trusted application code to persist."""

    event_type: EventType
    customer_id: str
    case_id: str | None
    order_id: str | None
    actor: Actor
    channel: Channel
    payload: BaseModel


def parse_event_type(event_type: str) -> EventType:
    """Parse a catalogue event type or raise UnknownEventTypeError."""
    try:
        return EventType(event_type)
    except ValueError as error:
        raise UnknownEventTypeError(event_type) from error


def payload_model_for_event_type(event_type: EventType) -> PayloadModel:
    """Return the payload model registered for an event type."""
    return PAYLOAD_MODEL_BY_EVENT_TYPE[event_type]


def validate_payload(
    event_type: EventType,
    payload_data: dict[str, Any] | BaseModel,
) -> BaseModel:
    """Validate payload data against the schema for an event type."""
    payload_model = payload_model_for_event_type(event_type)
    if isinstance(payload_data, BaseModel):
        return payload_model.model_validate(payload_data.model_dump())
    return payload_model.model_validate(payload_data)


def _expected_actor_channel(
    event_type: EventType,
    payload: BaseModel,
) -> tuple[Actor, Channel]:
    if event_type is EventType.CUSTOMER_VERIFIED:
        customer_verified = CustomerVerifiedPayload.model_validate(payload.model_dump())
        if customer_verified.method is VerificationMethod.SEED:
            return Actor.SYSTEM, Channel.INTERNAL
        return Actor.SYSTEM, Channel.VERIFICATION_API

    if event_type is EventType.ESCALATED:
        escalated = EscalatedPayload.model_validate(payload.model_dump())
        if escalated.origin is EscalationOrigin.AGENT:
            return Actor.AGENT, Channel.INTERNAL
        return Actor.SYSTEM, Channel.INTERNAL

    return _FIXED_ACTOR_CHANNEL_BY_EVENT_TYPE[event_type]


def _validate_event_scope(
    event_type: EventType,
    case_id: str | None,
    order_id: str | None,
) -> None:
    if event_type in _CUSTOMER_SCOPED_EVENT_TYPES:
        if case_id is not None or order_id is not None:
            message = (
                f"{event_type.value} is customer-scoped and must not carry "
                "case_id or order_id"
            )
            raise InvalidEventScopeError(message)
        return

    if event_type in _CASE_AND_ORDER_SCOPED_EVENT_TYPES:
        if case_id is None or order_id is None:
            message = f"{event_type.value} requires both case_id and order_id"
            raise InvalidEventScopeError(message)
        return

    if event_type in _CASE_SCOPED_EVENT_TYPES:
        if case_id is None:
            message = f"{event_type.value} requires case_id"
            raise InvalidEventScopeError(message)
        if order_id is not None:
            message = f"{event_type.value} is case-scoped and must not carry order_id"
            raise InvalidEventScopeError(message)


def build_canonical_event(  # noqa: PLR0913
    event_type: EventType | str,
    *,
    customer_id: str,
    case_id: str | None = None,
    order_id: str | None = None,
    actor: Actor,
    channel: Channel,
    payload: dict[str, Any] | BaseModel,
) -> CanonicalEvent:
    """Validate payload, scope, and actor/channel before returning a canonical event."""
    parsed_event_type = (
        event_type
        if isinstance(event_type, EventType)
        else parse_event_type(event_type)
    )
    validated_payload = validate_payload(parsed_event_type, payload)
    _validate_event_scope(parsed_event_type, case_id, order_id)

    expected_actor, expected_channel = _expected_actor_channel(
        parsed_event_type,
        validated_payload,
    )
    if actor is not expected_actor or channel is not expected_channel:
        raise InvalidActorChannelError(
            parsed_event_type,
            expected_actor,
            expected_channel,
            actor,
            channel,
        )

    return CanonicalEvent(
        event_type=parsed_event_type,
        customer_id=customer_id,
        case_id=case_id,
        order_id=order_id,
        actor=actor,
        channel=channel,
        payload=validated_payload,
    )


def payload_as_dict(canonical_event: CanonicalEvent) -> dict[str, Any]:
    """Serialize a validated payload for JSON persistence."""
    return canonical_event.payload.model_dump(mode="json")

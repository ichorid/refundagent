"""Helpers for hand-built projection event streams."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from saferefund.domain.enums import Actor, Channel, VerificationMethod
from saferefund.domain.events import EventType
from saferefund.projections.types import CustomerSeed, OrderSeed


@dataclass(frozen=True, slots=True)
class FoldEvent:
    """Hand-built event for projection unit tests (satisfies ``FoldableEvent``)."""

    customer_id: str
    case_id: str | None
    order_id: str | None
    seq: int
    event_type: EventType
    actor: Actor
    channel: Channel
    payload: dict[str, Any]


BASE_TIME = datetime(2030, 6, 1, 12, 0, tzinfo=UTC)
TEST_VERIFICATION_TOKEN = "vtok_test"


def customer_seed(
    customer_id: str = "cust_sophie",
    email: str = "sophie@example.com",
) -> CustomerSeed:
    return CustomerSeed(customer_id=customer_id, email=email)


def order_seed(
    order_id: str = "ORD-1001",
    customer_id: str = "cust_sophie",
    total: Decimal = Decimal("249.00"),
) -> OrderSeed:
    return OrderSeed(order_id=order_id, customer_id=customer_id, total=total)


def fold_event(  # noqa: PLR0913
    *,
    seq: int,
    event_type: EventType,
    actor: Actor,
    customer_id: str = "cust_sophie",
    case_id: str | None = None,
    order_id: str | None = None,
    channel: Channel = Channel.INTERNAL,
    payload: dict[str, Any] | None = None,
) -> FoldEvent:
    return FoldEvent(
        customer_id=customer_id,
        case_id=case_id,
        order_id=order_id,
        seq=seq,
        event_type=event_type,
        actor=actor,
        channel=channel,
        payload=payload or {},
    )


def customer_verified_event(
    *,
    seq: int,
    customer_id: str = "cust_sophie",
    method: VerificationMethod = VerificationMethod.SEED,
    channel: Channel = Channel.INTERNAL,
) -> FoldEvent:
    return fold_event(
        seq=seq,
        event_type=EventType.CUSTOMER_VERIFIED,
        actor=Actor.SYSTEM,
        customer_id=customer_id,
        case_id=None,
        order_id=None,
        channel=channel,
        payload={"method": method.value},
    )


def verification_requested_event(
    *,
    seq: int,
    case_id: str,
    customer_id: str = "cust_tom",
    verification_token: str = TEST_VERIFICATION_TOKEN,
    expires_at: datetime | None = None,
) -> FoldEvent:
    return fold_event(
        seq=seq,
        event_type=EventType.VERIFICATION_REQUESTED,
        actor=Actor.AGENT,
        customer_id=customer_id,
        case_id=case_id,
        payload={
            "token": verification_token,
            "expires_at": (expires_at or BASE_TIME).isoformat(),
        },
    )

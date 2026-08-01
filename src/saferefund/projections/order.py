"""Order-scoped refund history projection across all cases."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from saferefund.domain.events import EventType, validate_payload
from saferefund.domain.payloads import RefundExecutedPayload
from saferefund.projections.types import FoldableEvent, OrderSeed


@dataclass(frozen=True, slots=True)
class OrderSummary:
    """Folded refund history for one order, spanning every case."""

    order_id: str
    customer_id: str
    total: Decimal
    refunded_total: Decimal
    has_open_refund: bool

    @property
    def refundable_remainder(self) -> Decimal:
        """Remaining order total not yet covered by executed refunds."""
        return self.total - self.refunded_total


def _order_scoped_events(
    order_id: str,
    customer_id: str,
    events: Sequence[FoldableEvent],
) -> list[FoldableEvent]:
    return [
        event
        for event in events
        if event.order_id == order_id and event.customer_id == customer_id
    ]


def project_order_summary(
    order_seed: OrderSeed,
    events: Sequence[FoldableEvent],
    now: datetime,  # noqa: ARG001
) -> OrderSummary:
    """Fold order-scoped refund lifecycle events in ascending sequence order."""
    refunded_total = Decimal(0)
    has_open_refund = False

    for event in sorted(
        _order_scoped_events(order_seed.order_id, order_seed.customer_id, events),
        key=lambda fold_event: fold_event.seq,
    ):
        if event.event_type is EventType.REFUND_PROPOSED:
            validate_payload(event.event_type, dict(event.payload))
            has_open_refund = True
            continue

        if event.event_type is EventType.REFUND_EXECUTED:
            executed = RefundExecutedPayload.model_validate(
                validate_payload(event.event_type, dict(event.payload)).model_dump()
            )
            refunded_total += executed.amount
            has_open_refund = False
            continue

        if event.event_type in {
            EventType.REFUND_REJECTED,
            EventType.REFUND_EXPIRED,
        }:
            has_open_refund = False

    return OrderSummary(
        order_id=order_seed.order_id,
        customer_id=order_seed.customer_id,
        total=order_seed.total,
        refunded_total=refunded_total,
        has_open_refund=has_open_refund,
    )

from decimal import Decimal

from saferefund.domain.enums import Actor, Channel
from saferefund.domain.events import EventType
from saferefund.projections.order import project_order_summary
from tests.unit.projection_helpers import BASE_TIME, fold_event, order_seed


def test_order_starts_with_zero_refunded_total_and_no_open_refund() -> None:
    summary = project_order_summary(order_seed(), [], BASE_TIME)

    assert summary.refunded_total == Decimal(0)
    assert summary.has_open_refund is False
    assert summary.refundable_remainder == Decimal("249.00")


def test_refund_proposed_sets_open_refund_flag() -> None:
    summary = project_order_summary(
        order_seed(),
        [
            fold_event(
                seq=1,
                event_type=EventType.REFUND_PROPOSED,
                actor=Actor.AGENT,
                case_id="case_1",
                order_id="ORD-1001",
                payload={"refund_id": "rfnd_1", "amount": "100.00"},
            )
        ],
        BASE_TIME,
    )

    assert summary.has_open_refund is True
    assert summary.refunded_total == Decimal(0)


def test_refund_executed_accumulates_total_and_clears_open_refund() -> None:
    summary = project_order_summary(
        order_seed(),
        [
            fold_event(
                seq=1,
                event_type=EventType.REFUND_PROPOSED,
                actor=Actor.AGENT,
                case_id="case_1",
                order_id="ORD-1001",
                payload={"refund_id": "rfnd_1", "amount": "100.00"},
            ),
            fold_event(
                seq=2,
                event_type=EventType.REFUND_EXECUTED,
                actor=Actor.SYSTEM,
                case_id="case_1",
                order_id="ORD-1001",
                payload={
                    "refund_id": "rfnd_1",
                    "amount": "100.00",
                    "provider_ref": "pay_rfnd_1",
                },
            ),
        ],
        BASE_TIME,
    )

    assert summary.refunded_total == Decimal("100.00")
    assert summary.has_open_refund is False
    assert summary.refundable_remainder == Decimal("149.00")


def test_refund_rejected_clears_open_refund_without_changing_total() -> None:
    summary = project_order_summary(
        order_seed(),
        [
            fold_event(
                seq=1,
                event_type=EventType.REFUND_PROPOSED,
                actor=Actor.AGENT,
                case_id="case_1",
                order_id="ORD-1001",
                payload={"refund_id": "rfnd_1", "amount": "100.00"},
            ),
            fold_event(
                seq=2,
                event_type=EventType.REFUND_REJECTED,
                actor=Actor.OPERATOR,
                case_id="case_1",
                order_id="ORD-1001",
                channel=Channel.OPERATOR_API,
                payload={
                    "refund_id": "rfnd_1",
                    "operator_id": "op_1",
                    "reason": "ignored",
                },
            ),
        ],
        BASE_TIME,
    )

    assert summary.has_open_refund is False
    assert summary.refunded_total == Decimal(0)


def test_refund_expired_clears_open_refund() -> None:
    summary = project_order_summary(
        order_seed(),
        [
            fold_event(
                seq=1,
                event_type=EventType.REFUND_PROPOSED,
                actor=Actor.AGENT,
                case_id="case_1",
                order_id="ORD-1001",
                payload={"refund_id": "rfnd_1", "amount": "100.00"},
            ),
            fold_event(
                seq=2,
                event_type=EventType.REFUND_EXPIRED,
                actor=Actor.SYSTEM,
                case_id="case_1",
                order_id="ORD-1001",
                payload={"refund_id": "rfnd_1"},
            ),
        ],
        BASE_TIME,
    )

    assert summary.has_open_refund is False


def test_order_projection_accumulates_refunds_across_cases() -> None:
    summary = project_order_summary(
        order_seed(order_id="ORD-1003", total=Decimal("780.00")),
        [
            fold_event(
                seq=1,
                event_type=EventType.REFUND_EXECUTED,
                actor=Actor.SYSTEM,
                case_id="case_a",
                order_id="ORD-1003",
                payload={
                    "refund_id": "rfnd_a",
                    "amount": "300.00",
                    "provider_ref": "pay_rfnd_a",
                },
            ),
            fold_event(
                seq=5,
                event_type=EventType.REFUND_EXECUTED,
                actor=Actor.SYSTEM,
                case_id="case_b",
                order_id="ORD-1003",
                payload={
                    "refund_id": "rfnd_b",
                    "amount": "200.00",
                    "provider_ref": "pay_rfnd_b",
                },
            ),
        ],
        BASE_TIME,
    )

    assert summary.refunded_total == Decimal("500.00")
    assert summary.refundable_remainder == Decimal("280.00")


def test_order_projection_ignores_events_for_other_orders() -> None:
    summary = project_order_summary(
        order_seed(order_id="ORD-1001"),
        [
            fold_event(
                seq=1,
                event_type=EventType.REFUND_EXECUTED,
                actor=Actor.SYSTEM,
                case_id="case_1",
                order_id="ORD-1002",
                payload={
                    "refund_id": "rfnd_other",
                    "amount": "24.00",
                    "provider_ref": "pay_rfnd_other",
                },
            )
        ],
        BASE_TIME,
    )

    assert summary.refunded_total == Decimal(0)


def test_order_projection_ignores_customer_mismatched_order_events() -> None:
    """Imported malformed history must not contaminate order refund totals."""
    summary = project_order_summary(
        order_seed(order_id="ORD-1001", customer_id="cust_sophie"),
        [
            fold_event(
                seq=1,
                event_type=EventType.REFUND_PROPOSED,
                actor=Actor.AGENT,
                customer_id="cust_tom",
                case_id="case_tom",
                order_id="ORD-1001",
                payload={"refund_id": "rfnd_foreign", "amount": "60.00"},
            ),
            fold_event(
                seq=2,
                event_type=EventType.REFUND_EXECUTED,
                actor=Actor.SYSTEM,
                customer_id="cust_tom",
                case_id="case_tom",
                order_id="ORD-1001",
                payload={
                    "refund_id": "rfnd_foreign",
                    "amount": "60.00",
                    "provider_ref": "pay_rfnd_foreign",
                },
            ),
        ],
        BASE_TIME,
    )

    assert summary.refunded_total == Decimal(0)
    assert summary.has_open_refund is False

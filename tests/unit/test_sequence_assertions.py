"""Mutation guards for shared exact event-sequence assertion helpers."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from saferefund.adapters import mailer, payment
from saferefund.domain.events import EventType
from tests.support.sequence_assertions import (
    assert_exact_event_type_sequence,
    assert_operator_approve_response_lifecycle,
    assert_operator_reject_response_lifecycle,
    denial_loop_termination_sequence,
)


@dataclass(frozen=True)
class _EventStub:
    event_type: EventType
    payload: dict[str, Any] | None = None
    order_id: str | None = None


def _stub_events(types: list[EventType]) -> list[_EventStub]:
    return [_EventStub(event_type=event_type) for event_type in types]


def _operator_approve_stub_events(
    *,
    refund_id: str,
    amount: Decimal,
    operator_id: str,
    order_id: str,
) -> list[_EventStub]:
    amount_text = format(amount, "f")
    return [
        _EventStub(event_type=EventType.CASE_OPENED),
        _EventStub(event_type=EventType.ORDERS_LISTED),
        _EventStub(event_type=EventType.ORDER_LINKED, order_id=order_id),
        _EventStub(
            event_type=EventType.REFUND_PROPOSED,
            order_id=order_id,
            payload={"refund_id": refund_id, "amount": amount_text},
        ),
        _EventStub(
            event_type=EventType.REFUND_APPROVAL_REQUIRED,
            order_id=order_id,
            payload={
                "refund_id": refund_id,
                "amount": amount_text,
                "rule": "R_THRESHOLD",
            },
        ),
        _EventStub(
            event_type=EventType.REFUND_APPROVED,
            order_id=order_id,
            payload={"refund_id": refund_id, "operator_id": operator_id},
        ),
        _EventStub(
            event_type=EventType.REFUND_EXECUTED,
            order_id=order_id,
            payload={
                "refund_id": refund_id,
                "amount": amount_text,
                "provider_ref": f"pay_{refund_id}",
            },
        ),
        _EventStub(event_type=EventType.REPLY_SENT),
        _EventStub(event_type=EventType.CASE_CLOSED),
    ]


def test_exact_sequence_helper_rejects_insertions_deletions_and_reordering() -> None:
    """Mutation: weaken to prefix, membership, or relative-order checks."""
    expected = [
        EventType.CASE_OPENED,
        EventType.EMAIL_RECEIVED,
        EventType.REFUND_EXPIRED,
        EventType.CASE_CLOSED,
    ]
    actual_events = _stub_events(expected)

    assert_exact_event_type_sequence(actual_events, expected)

    with pytest.raises(AssertionError, match="event-type sequence mismatch"):
        assert_exact_event_type_sequence(
            actual_events,
            [
                EventType.CASE_OPENED,
                EventType.REPLY_SENT,
                EventType.EMAIL_RECEIVED,
                EventType.REFUND_EXPIRED,
                EventType.CASE_CLOSED,
            ],
        )

    with pytest.raises(AssertionError, match="event-type sequence mismatch"):
        assert_exact_event_type_sequence(
            actual_events,
            [
                EventType.CASE_OPENED,
                EventType.EMAIL_RECEIVED,
                EventType.REFUND_EXPIRED,
            ],
        )

    with pytest.raises(AssertionError, match="event-type sequence mismatch"):
        assert_exact_event_type_sequence(
            actual_events,
            [
                EventType.CASE_OPENED,
                EventType.REFUND_EXPIRED,
                EventType.EMAIL_RECEIVED,
                EventType.CASE_CLOSED,
            ],
        )


def test_terminal_sequence_helper_rejects_nonterminal_insertions() -> None:
    """Mutation: weaken termination proofs to actor-filtered counts or adjacency."""
    expected = list(denial_loop_termination_sequence())
    actual_events = _stub_events(expected)

    assert_exact_event_type_sequence(actual_events, expected)

    with pytest.raises(AssertionError, match="event-type sequence mismatch"):
        assert_exact_event_type_sequence(
            actual_events,
            [
                EventType.CASE_OPENED,
                EventType.EMAIL_RECEIVED,
                EventType.REPLY_SENT,
                EventType.ACTION_DENIED,
                EventType.ACTION_DENIED,
                EventType.ACTION_DENIED,
                EventType.ESCALATED,
                EventType.CASE_CLOSED,
            ],
        )

    with pytest.raises(AssertionError, match="event-type sequence mismatch"):
        assert_exact_event_type_sequence(
            actual_events,
            [
                EventType.CASE_OPENED,
                EventType.EMAIL_RECEIVED,
                EventType.ACTION_DENIED,
                EventType.ACTION_DENIED,
                EventType.ACTION_DENIED,
                EventType.ESCALATED,
                EventType.REPLY_SENT,
                EventType.CASE_CLOSED,
            ],
        )

    with pytest.raises(AssertionError, match="event-type sequence mismatch"):
        assert_exact_event_type_sequence(
            actual_events,
            [
                EventType.CASE_OPENED,
                EventType.EMAIL_RECEIVED,
                EventType.ACTION_DENIED,
                EventType.ACTION_DENIED,
                EventType.ACTION_DENIED,
                EventType.ESCALATED,
            ],
        )


def test_operator_sequence_helper_rejects_wrong_identity_amount_order_and_effect_count() -> (  # noqa: E501
    None
):
    """Mutation: weaken operator HTTP proofs to status codes or partial joins."""
    refund_id = "rfnd_operator_guard"
    amount = Decimal("780.00")
    operator_id = "op-guard"
    order_id = "ORD-1003"
    actual_events = _operator_approve_stub_events(
        refund_id=refund_id,
        amount=amount,
        operator_id=operator_id,
        order_id=order_id,
    )
    payment.calls.clear()
    mailer.outbox.clear()
    payment.calls.append(
        payment.RefundCall(idempotency_key=refund_id, amount=amount),
    )
    try:
        assert_operator_approve_response_lifecycle(
            actual_events,
            refund_id=refund_id,
            amount=amount,
            operator_id=operator_id,
            order_id=order_id,
            mailer_messages=[],
        )

        with pytest.raises(AssertionError):
            assert_operator_approve_response_lifecycle(
                actual_events,
                refund_id="rfnd_wrong",
                amount=amount,
                operator_id=operator_id,
                order_id=order_id,
                mailer_messages=[],
            )

        with pytest.raises(AssertionError):
            assert_operator_approve_response_lifecycle(
                actual_events,
                refund_id=refund_id,
                amount=Decimal("1.00"),
                operator_id=operator_id,
                order_id=order_id,
                mailer_messages=[],
            )

        reordered_events = [
            *_operator_approve_stub_events(
                refund_id=refund_id,
                amount=amount,
                operator_id=operator_id,
                order_id=order_id,
            )[:5],
            _EventStub(
                event_type=EventType.REFUND_EXECUTED,
                order_id=order_id,
                payload={
                    "refund_id": refund_id,
                    "amount": format(amount, "f"),
                    "provider_ref": f"pay_{refund_id}",
                },
            ),
            _EventStub(
                event_type=EventType.REFUND_APPROVED,
                order_id=order_id,
                payload={
                    "refund_id": refund_id,
                    "operator_id": operator_id,
                },
            ),
            _EventStub(event_type=EventType.REPLY_SENT),
            _EventStub(event_type=EventType.CASE_CLOSED),
        ]
        with pytest.raises(AssertionError, match="event-type sequence mismatch"):
            assert_operator_approve_response_lifecycle(
                reordered_events,
                refund_id=refund_id,
                amount=amount,
                operator_id=operator_id,
                order_id=order_id,
                mailer_messages=[],
            )

        payment.calls.append(
            payment.RefundCall(idempotency_key=refund_id, amount=amount),
        )
        with pytest.raises(AssertionError):
            assert_operator_approve_response_lifecycle(
                actual_events,
                refund_id=refund_id,
                amount=amount,
                operator_id=operator_id,
                order_id=order_id,
                mailer_messages=[],
            )

        with pytest.raises(AssertionError):
            assert_operator_reject_response_lifecycle(
                actual_events,
                refund_id=refund_id,
                amount=amount,
                operator_id=operator_id,
                order_id=order_id,
                reason="not justified",
                mailer_messages=[],
            )
    finally:
        payment.calls.clear()
        mailer.outbox.clear()

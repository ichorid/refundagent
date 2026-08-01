"""Refund authorization intent must remain immutable through approval and payment.

The enforcement row is mutable only along documented lifecycle fields. Intent
fields bind operator approval and payment to canonical refund_proposed evidence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, cast

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from saferefund.adapters import payment
from saferefund.domain.enums import Actor, Channel, RefundStatus
from saferefund.domain.events import EventType
from saferefund.domain.payloads import RefundProposedPayload
from saferefund.domain.tables import CaseRow, RefundRow
from saferefund.gate.operations import approve_refund
from saferefund.repositories.events import append_canonical_event, load_case_events
from saferefund.repositories.refund_intent import RefundIntentIntegrityError
from saferefund.repositories.refund_transitions import (
    RefundStatusTransitionError,
    allowed_refund_status_targets,
    assert_refund_status_transition_permitted,
)
from saferefund.repositories.seed import SOPHIE_CUSTOMER_ID
from tests.invariants.scenario import propose_refund_awaiting_approval

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _refund_intent_immutable_error() -> type[Exception]:
    from saferefund.domain import tables

    error_type = getattr(tables, "RefundIntentImmutableError", None)
    assert error_type is not None, (
        "domain.tables must define RefundIntentImmutableError and raise it when "
        "refund authorization intent fields are mutated"
    )
    return cast("type[Exception]", error_type)


async def _commit_refund_field_mutation(
    session_factory: async_sessionmaker[AsyncSession],
    refund_id: str,
    field_name: str,
    mutated_value: object,
) -> None:
    async with session_factory.begin() as session:
        refund_row = await session.get(RefundRow, refund_id)
        assert refund_row is not None
        setattr(refund_row, field_name, mutated_value)


async def _commit_refund_amount_mutation(
    session_factory: async_sessionmaker[AsyncSession],
    refund_id: str,
    amount: Decimal,
) -> None:
    await _commit_refund_field_mutation(session_factory, refund_id, "amount", amount)


async def _commit_status_and_amount_mutation(
    session_factory: async_sessionmaker[AsyncSession],
    refund_id: str,
) -> None:
    async with session_factory.begin() as session:
        refund_row = await session.get(RefundRow, refund_id)
        assert refund_row is not None
        refund_row.status = RefundStatus.APPROVED
        refund_row.amount = Decimal("9999.00")


async def test_operator_approval_cannot_pay_a_mutated_proposal_amount(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The amount shown for approval must remain bound to the eventual payment."""
    proposed_amount = Decimal("780.00")
    refund_id = await propose_refund_awaiting_approval(
        api_session_factory,
        case_id="review-mutable-refund-amount",
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="review-mutable-refund-amount",
        amount=proposed_amount,
    )

    intent_immutable_error = _refund_intent_immutable_error()
    with pytest.raises(intent_immutable_error):
        await _commit_refund_amount_mutation(
            api_session_factory,
            refund_id,
            Decimal("9999.00"),
        )

    async with api_session_factory.begin() as session:
        await approve_refund(
            session,
            refund_id,
            "review-operator",
            session_factory=api_session_factory,
        )

    refund_calls = [call for call in payment.calls if call.idempotency_key == refund_id]
    assert len(refund_calls) == 1
    assert refund_calls[0].amount == proposed_amount


@pytest.mark.parametrize(
    ("field_name", "mutated_value"),
    [
        ("id", "rfnd_tampered"),
        ("customer_id", "cust_tom"),
        ("order_id", "ORD-9999"),
        ("case_id", "case_tampered"),
        ("amount", Decimal("9999.00")),
        ("created_at", datetime(1999, 1, 1, tzinfo=UTC)),
    ],
)
async def test_refund_identity_fields_are_immutable(
    api_session_factory: async_sessionmaker[AsyncSession],
    field_name: str,
    mutated_value: object,
) -> None:
    """Each refund authorization intent field rejects ORM mutation before commit."""
    refund_id = await propose_refund_awaiting_approval(
        api_session_factory,
        case_id=f"case-immutable-{field_name}",
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id=f"msg-immutable-{field_name}",
    )

    intent_immutable_error = _refund_intent_immutable_error()
    with pytest.raises(intent_immutable_error):
        await _commit_refund_field_mutation(
            api_session_factory,
            refund_id,
            field_name,
            mutated_value,
        )


async def test_status_transition_cannot_change_intent_fields(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Lifecycle status changes must not smuggle intent-field mutations."""
    refund_id = await propose_refund_awaiting_approval(
        api_session_factory,
        case_id="case-status-plus-intent-mutation",
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-status-plus-intent-mutation",
    )

    intent_immutable_error = _refund_intent_immutable_error()
    with pytest.raises(intent_immutable_error):
        await _commit_status_and_amount_mutation(api_session_factory, refund_id)


async def test_raw_sql_row_event_disagreement_fails_closed_before_approval(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A row mutated outside the ORM must not approve or pay against evidence."""
    proposed_amount = Decimal("780.00")
    refund_id = await propose_refund_awaiting_approval(
        api_session_factory,
        case_id="case-raw-sql-disagreement",
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-raw-sql-disagreement",
        amount=proposed_amount,
    )
    payment.calls.clear()

    async with api_session_factory.begin() as session:
        await session.execute(
            text("UPDATE refunds SET amount = :amount WHERE id = :refund_id"),
            {"amount": "9999.00", "refund_id": refund_id},
        )

    with pytest.raises(RefundIntentIntegrityError):
        async with api_session_factory.begin() as session:
            await approve_refund(
                session,
                refund_id,
                "review-operator-raw-sql",
                session_factory=api_session_factory,
            )

    assert [call for call in payment.calls if call.idempotency_key == refund_id] == []

    async with api_session_factory() as session:
        case_events = await load_case_events(session, "case-raw-sql-disagreement")
    assert EventType.REFUND_APPROVED not in [event.event_type for event in case_events]


@pytest.mark.parametrize(
    ("tamper_sql", "bind_params"),
    [
        (
            (
                "UPDATE events SET customer_id = :value "
                "WHERE case_id = :case_id AND type = :event_type"
            ),
            {
                "value": "cust_tom",
                "case_id": "case-event-customer",
                "event_type": "refund_proposed",
            },
        ),
        (
            (
                "UPDATE events SET case_id = :value "
                "WHERE case_id = :case_id AND type = :event_type"
            ),
            {
                "value": "case-event-case-alt",
                "case_id": "case-event-case",
                "event_type": "refund_proposed",
            },
        ),
        (
            (
                "UPDATE events SET order_id = :value "
                "WHERE case_id = :case_id AND type = :event_type"
            ),
            {
                "value": "ORD-2001",
                "case_id": "case-event-order",
                "event_type": "refund_proposed",
            },
        ),
    ],
)
async def test_event_identity_disagreement_fails_closed_before_approval(
    api_session_factory: async_sessionmaker[AsyncSession],
    tamper_sql: str,
    bind_params: dict[str, str],
) -> None:
    """Each canonical identity dimension must match the enforcement row."""
    case_id = bind_params["case_id"]
    if bind_params.get("value") == "case-event-case-alt":
        async with api_session_factory.begin() as session:
            session.add(
                CaseRow(
                    id="case-event-case-alt",
                    customer_id=SOPHIE_CUSTOMER_ID,
                    opening_message_id="msg-case-event-case-alt",
                    created_at=datetime(2030, 1, 2, tzinfo=UTC),
                )
            )
            await append_canonical_event(
                session,
                event_type=EventType.CASE_OPENED,
                customer_id=SOPHIE_CUSTOMER_ID,
                case_id="case-event-case-alt",
                actor=Actor.SYSTEM,
                channel=Channel.INTERNAL,
                payload={"opening_message_id": "msg-case-event-case-alt"},
            )
    refund_id = await propose_refund_awaiting_approval(
        api_session_factory,
        case_id=case_id,
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id=f"msg-{case_id}",
    )
    payment.calls.clear()

    tamper_blocked_by_relational_scope = bind_params.get("value") in {
        "cust_tom",
        "ORD-2001",
    }
    if tamper_blocked_by_relational_scope:
        with pytest.raises(IntegrityError):
            async with api_session_factory.begin() as session:
                await session.execute(text(tamper_sql), bind_params)
        assert [
            call for call in payment.calls if call.idempotency_key == refund_id
        ] == []
        async with api_session_factory() as session:
            case_events = await load_case_events(session, case_id)
        assert EventType.REFUND_APPROVED not in [
            event.event_type for event in case_events
        ]
        return

    async with api_session_factory.begin() as session:
        await session.execute(text(tamper_sql), bind_params)

    with pytest.raises(RefundIntentIntegrityError):
        async with api_session_factory.begin() as session:
            await approve_refund(
                session,
                refund_id,
                "review-operator-event-disagreement",
                session_factory=api_session_factory,
            )

    assert [call for call in payment.calls if call.idempotency_key == refund_id] == []

    async with api_session_factory() as session:
        case_events = await load_case_events(session, case_id)
    assert EventType.REFUND_APPROVED not in [event.event_type for event in case_events]


async def test_event_amount_disagreement_fails_closed_before_approval(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Proposal evidence amount must equal the enforcement row before approval."""
    refund_id = await propose_refund_awaiting_approval(
        api_session_factory,
        case_id="case-event-amount",
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-event-amount",
        amount=Decimal("780.00"),
    )
    payment.calls.clear()

    async with api_session_factory.begin() as session:
        case_events = await load_case_events(session, "case-event-amount")
        proposed_event = next(
            event
            for event in case_events
            if event.event_type is EventType.REFUND_PROPOSED
        )
        proposed_payload = RefundProposedPayload.model_validate(proposed_event.payload)
        tampered_payload = proposed_payload.model_copy(
            update={"amount": Decimal("9999.00")},
        )
        await session.execute(
            text("UPDATE events SET payload = :payload WHERE id = :event_id"),
            {
                "payload": json.dumps(tampered_payload.model_dump(mode="json")),
                "event_id": proposed_event.id,
            },
        )

    with pytest.raises(RefundIntentIntegrityError):
        async with api_session_factory.begin() as session:
            await approve_refund(
                session,
                refund_id,
                "review-operator-event-amount",
                session_factory=api_session_factory,
            )

    assert [call for call in payment.calls if call.idempotency_key == refund_id] == []

    async with api_session_factory() as session:
        case_events = await load_case_events(session, "case-event-amount")
    assert EventType.REFUND_APPROVED not in [event.event_type for event in case_events]


async def test_duplicate_refund_proposed_evidence_fails_closed_before_approval(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ambiguous refund_proposed evidence must fail closed before approval."""
    case_id = "case-duplicate-proposed"
    refund_id = await propose_refund_awaiting_approval(
        api_session_factory,
        case_id=case_id,
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-duplicate-proposed",
    )
    payment.calls.clear()

    async with api_session_factory.begin() as session:
        case_events = await load_case_events(session, case_id)
        proposed_event = next(
            event
            for event in case_events
            if event.event_type is EventType.REFUND_PROPOSED
        )
        await append_canonical_event(
            session,
            event_type=EventType.REFUND_PROPOSED,
            customer_id=proposed_event.customer_id,
            case_id=proposed_event.case_id,
            order_id=proposed_event.order_id,
            actor=Actor.AGENT,
            channel=Channel.INTERNAL,
            payload=RefundProposedPayload(
                refund_id=refund_id,
                amount=Decimal("1.00"),
            ),
        )

    with pytest.raises(RefundIntentIntegrityError):
        async with api_session_factory.begin() as session:
            await approve_refund(
                session,
                refund_id,
                "review-operator-duplicate-proposed",
                session_factory=api_session_factory,
            )

    assert [call for call in payment.calls if call.idempotency_key == refund_id] == []

    async with api_session_factory() as session:
        case_events = await load_case_events(session, case_id)
    assert EventType.REFUND_APPROVED not in [event.event_type for event in case_events]


def test_refund_status_transition_matrix_documents_allowed_edges() -> None:
    """Only documented lifecycle edges are permitted by guarded SQL."""
    assert allowed_refund_status_targets(RefundStatus.PENDING_APPROVAL) == frozenset(
        {
            RefundStatus.APPROVED,
            RefundStatus.REJECTED,
            RefundStatus.EXPIRED,
        }
    )
    assert allowed_refund_status_targets(RefundStatus.APPROVED) == frozenset(
        {RefundStatus.EXECUTED}
    )
    assert allowed_refund_status_targets(RefundStatus.EXECUTED) == frozenset()

    with pytest.raises(RefundStatusTransitionError):
        assert_refund_status_transition_permitted(
            RefundStatus.EXECUTED,
            RefundStatus.PENDING_APPROVAL,
        )

    with pytest.raises(RefundStatusTransitionError):
        assert_refund_status_transition_permitted(
            RefundStatus.REJECTED,
            RefundStatus.APPROVED,
        )


async def test_bound_approval_update_rejects_mutated_row_without_payment(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Guarded approval SQL must not win when immutable binding no longer matches."""
    proposed_amount = Decimal("780.00")
    refund_id = await propose_refund_awaiting_approval(
        api_session_factory,
        case_id="case-bound-approval",
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-bound-approval",
        amount=proposed_amount,
    )
    payment.calls.clear()

    async with api_session_factory.begin() as session:
        await session.execute(
            text("UPDATE refunds SET amount = :amount WHERE id = :refund_id"),
            {"amount": "9999.00", "refund_id": refund_id},
        )

    with pytest.raises(RefundIntentIntegrityError):
        async with api_session_factory.begin() as session:
            await approve_refund(
                session,
                refund_id,
                "review-operator-bound",
                session_factory=api_session_factory,
            )

    async with api_session_factory() as session:
        refund_row = await session.get(RefundRow, refund_id)
        assert refund_row is not None
        assert refund_row.status is RefundStatus.PENDING_APPROVAL
        assert refund_row.amount == Decimal("9999.00")

    assert [call for call in payment.calls if call.idempotency_key == refund_id] == []


async def test_approval_expires_at_may_change_without_intent_mutation(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Lifecycle-only fields remain mutable through the ORM guard."""
    refund_id = await propose_refund_awaiting_approval(
        api_session_factory,
        case_id="case-lifecycle-field",
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-lifecycle-field",
    )

    new_expiry = datetime(2030, 2, 1, tzinfo=UTC)
    async with api_session_factory.begin() as session:
        refund_row = await session.get(RefundRow, refund_id)
        assert refund_row is not None
        refund_row.approval_expires_at = new_expiry

    async with api_session_factory() as session:
        refund_row = await session.get(RefundRow, refund_id)
        assert refund_row is not None
        stored_expiry = refund_row.approval_expires_at
        assert stored_expiry is not None
        if stored_expiry.tzinfo is None:
            stored_expiry = stored_expiry.replace(tzinfo=UTC)
        assert stored_expiry == new_expiry


async def _commit_refund_status_mutation(
    session_factory: async_sessionmaker[AsyncSession],
    refund_id: str,
    status: RefundStatus,
) -> None:
    async with session_factory.begin() as session:
        refund_row = await session.get(RefundRow, refund_id)
        assert refund_row is not None
        refund_row.status = status


async def test_orm_cannot_bypass_refund_status_transition_matrix(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Ordinary ORM writes must not bypass the guarded lifecycle transition API."""
    refund_id = await propose_refund_awaiting_approval(
        api_session_factory,
        case_id="case-orm-status-bypass",
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-orm-status-bypass",
    )

    with pytest.raises(RefundStatusTransitionError):
        await _commit_refund_status_mutation(
            api_session_factory,
            refund_id,
            RefundStatus.EXECUTED,
        )

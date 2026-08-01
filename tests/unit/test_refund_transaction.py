"""Unit tests for refund transaction ordering, races, and payment idempotency."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund import clock, config
from saferefund.actions.models import GetOrders, LinkOrder, ProposeRefund
from saferefund.adapters import payment, reset_adapters_for_tests
from saferefund.adapters.payment import RefundResult
from saferefund.domain.enums import Actor, Channel, RefundStatus
from saferefund.domain.events import EventType
from saferefund.domain.payloads import (
    ActionDeniedPayload,
    RefundExecutedPayload,
    RefundProposedPayload,
)
from saferefund.domain.tables import CaseRow, RefundRow
from saferefund.gate.operations import execute_agent_action
from saferefund.gate.refund import quantize_refund_amount
from saferefund.policy.verdicts import Allow, Deny
from saferefund.repositories.events import append_canonical_event, load_case_events
from saferefund.repositories.refunds import (
    find_open_refund_for_order,
    find_refund_by_id,
)
from saferefund.repositories.seed import ORD_1001_ID, ORD_1003_ID, SOPHIE_CUSTOMER_ID


async def _open_case(
    session: AsyncSession,
    *,
    case_id: str,
    customer_id: str,
    opening_message_id: str,
) -> None:
    session.add(
        CaseRow(
            id=case_id,
            customer_id=customer_id,
            opening_message_id=opening_message_id,
            created_at=datetime(2030, 1, 1, tzinfo=UTC),
        )
    )
    await append_canonical_event(
        session,
        event_type=EventType.CASE_OPENED,
        customer_id=customer_id,
        case_id=case_id,
        actor=Actor.SYSTEM,
        channel=Channel.INTERNAL,
        payload={"opening_message_id": opening_message_id},
    )


async def _link_order(
    session: AsyncSession,
    *,
    case_id: str,
    order_id: str,
) -> None:
    await execute_agent_action(session, case_id, GetOrders(action="get_orders"))
    await execute_agent_action(
        session,
        case_id,
        LinkOrder(action="link_order", order_id=order_id),
    )


@pytest.fixture
def refund_transaction_session_factory(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    reset_adapters_for_tests()
    return seeded_session_factory


def test_quantize_refund_amount_rounds_to_two_decimal_places() -> None:
    assert quantize_refund_amount(Decimal("10.005")) == Decimal("10.01")
    assert quantize_refund_amount(Decimal("10.004")) == Decimal("10.00")


async def test_payment_idempotency_key_equals_refund_id(
    refund_transaction_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with refund_transaction_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_sophie",
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-idempotency",
        )
        await _link_order(session, case_id="case_sophie", order_id=ORD_1001_ID)
        await execute_agent_action(
            session,
            "case_sophie",
            ProposeRefund(action="propose_refund", amount=Decimal("50.00")),
        )

    assert len(payment.calls) == 1

    async with refund_transaction_session_factory() as session:
        case_events = await load_case_events(session, "case_sophie")
        proposed = next(
            event
            for event in case_events
            if event.event_type is EventType.REFUND_PROPOSED
        )
        proposed_payload = RefundProposedPayload.model_validate(proposed.payload)
        assert payment.calls[0].idempotency_key == proposed_payload.refund_id


async def test_post_payment_event_appends_with_monotonic_customer_seq(
    refund_transaction_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with refund_transaction_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_sophie",
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-seq",
        )
        await _link_order(session, case_id="case_sophie", order_id=ORD_1001_ID)
        await execute_agent_action(
            session,
            "case_sophie",
            ProposeRefund(action="propose_refund", amount=Decimal("25.00")),
        )

    async with refund_transaction_session_factory() as session:
        case_events = await load_case_events(session, "case_sophie")
        seq_values = [event.seq for event in case_events]
        assert seq_values == sorted(seq_values)
        assert len(set(seq_values)) == len(seq_values)

        executed_event = next(
            event
            for event in case_events
            if event.event_type is EventType.REFUND_EXECUTED
        )
        auto_approved_event = next(
            event
            for event in case_events
            if event.event_type is EventType.REFUND_AUTO_APPROVED
        )
        assert executed_event.seq == auto_approved_event.seq + 1
        executed_payload = RefundExecutedPayload.model_validate(executed_event.payload)
        assert executed_payload.provider_ref.startswith("pay_")


async def test_open_refund_unique_index_race_translates_to_denial(
    refund_transaction_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Simulate TOCTOU race: row exists but projection has no open refund."""
    async with refund_transaction_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_sophie",
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-race",
        )
        await _link_order(session, case_id="case_sophie", order_id=ORD_1001_ID)
        session.add(
            RefundRow(
                id="rfnd_race_existing",
                customer_id=SOPHIE_CUSTOMER_ID,
                order_id=ORD_1001_ID,
                case_id="case_sophie",
                amount=Decimal("5.00"),
                status=RefundStatus.PENDING_APPROVAL,
                approval_expires_at=clock.now()
                + timedelta(seconds=config.APPROVAL_TTL_SECONDS),
                created_at=clock.now(),
            )
        )
        await session.flush()
        verdict = await execute_agent_action(
            session,
            "case_sophie",
            ProposeRefund(action="propose_refund", amount=Decimal("10.00")),
        )

    assert isinstance(verdict, Deny)
    assert verdict.rule == "R_OPEN_REFUND"
    assert len(payment.calls) == 0

    async with refund_transaction_session_factory() as session:
        case_events = await load_case_events(session, "case_sophie")
        denied_event = case_events[-1]
        assert denied_event.event_type is EventType.ACTION_DENIED
        denied_payload = ActionDeniedPayload.model_validate(denied_event.payload)
        assert denied_payload.rule == "R_OPEN_REFUND"
        assert denied_payload.action == "propose_refund"

        refund_rows = await session.scalars(
            select(RefundRow).where(RefundRow.order_id == ORD_1001_ID)
        )
        assert len(list(refund_rows)) == 1
        assert (await find_refund_by_id(session, "rfnd_race_existing")) is not None


async def test_unrelated_integrity_error_propagates_without_r_open_refund_denial(
    refund_transaction_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrelated constraint failure must not be translated to R_OPEN_REFUND.

    Mutation: remove the find_open_refund_for_order guard in execute_propose_refund
    so every IntegrityError becomes R_OPEN_REFUND; this test fails.
    """
    unrelated_error = IntegrityError(
        statement="INSERT INTO events",
        params={},
        orig=Exception("uq_event_customer_seq"),
    )

    async def raise_on_refund_proposed_event(*_args: object, **_kwargs: object) -> None:
        raise unrelated_error

    monkeypatch.setattr(
        "saferefund.gate.refund._append_refund_proposed",
        raise_on_refund_proposed_event,
    )

    async with refund_transaction_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_sophie",
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-unrelated-integrity",
        )
        await _link_order(session, case_id="case_sophie", order_id=ORD_1003_ID)
        with pytest.raises(IntegrityError):
            await execute_agent_action(
                session,
                "case_sophie",
                ProposeRefund(action="propose_refund", amount=Decimal("50.00")),
            )

    assert len(payment.calls) == 0

    async with refund_transaction_session_factory() as session:
        assert await find_open_refund_for_order(session, ORD_1003_ID) is None
        case_events = await load_case_events(session, "case_sophie")
        denied_rules = [
            ActionDeniedPayload.model_validate(event.payload).rule
            for event in case_events
            if event.event_type is EventType.ACTION_DENIED
        ]
        assert "R_OPEN_REFUND" not in denied_rules


async def test_unquantised_amount_is_quantised_only_at_persistence(
    refund_transaction_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with refund_transaction_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_sophie",
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-quantize",
        )
        await _link_order(session, case_id="case_sophie", order_id=ORD_1001_ID)
        verdict = await execute_agent_action(
            session,
            "case_sophie",
            ProposeRefund(action="propose_refund", amount=Decimal("10.1")),
        )

    assert verdict == Allow()
    assert payment.calls[0].amount == Decimal("10.10")

    async with refund_transaction_session_factory() as session:
        case_events = await load_case_events(session, "case_sophie")
        proposed = next(
            event
            for event in case_events
            if event.event_type is EventType.REFUND_PROPOSED
        )
        proposed_payload = RefundProposedPayload.model_validate(proposed.payload)
        assert proposed_payload.amount == Decimal("10.10")


async def test_payment_observes_committed_refund_not_flushed_state(
    refund_transaction_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove payment runs only after refund intent is committed, not merely flushed."""
    intent_visible_from_separate_session: list[bool] = []
    original_refund = payment.refund

    async def refund_after_commit_check(
        *,
        idempotency_key: str,
        amount: Decimal,
    ) -> RefundResult:
        async with refund_transaction_session_factory() as check_session:
            refund_row = await find_refund_by_id(check_session, idempotency_key)
            intent_visible_from_separate_session.append(
                refund_row is not None and refund_row.status is RefundStatus.APPROVED,
            )
        return await original_refund(idempotency_key=idempotency_key, amount=amount)

    monkeypatch.setattr(
        payment,
        "refund",
        refund_after_commit_check,
    )

    async with refund_transaction_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_sophie",
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-committed-intent",
        )
        await _link_order(session, case_id="case_sophie", order_id=ORD_1001_ID)
        verdict = await execute_agent_action(
            session,
            "case_sophie",
            ProposeRefund(action="propose_refund", amount=Decimal("15.00")),
        )

    assert verdict == Allow()
    assert intent_visible_from_separate_session == [True]

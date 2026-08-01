"""Unit tests for refund proposal dispatch through execute_agent_action."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund.actions.models import GetOrders, LinkOrder, ProposeRefund
from saferefund.adapters import payment, reset_adapters_for_tests
from saferefund.domain.enums import Actor, CaseStatus, Channel, RefundStatus
from saferefund.domain.events import EventType
from saferefund.domain.payloads import (
    RefundApprovalRequiredPayload,
    RefundAutoApprovedPayload,
    RefundExecutedPayload,
    RefundProposedPayload,
)
from saferefund.domain.tables import CaseRow
from saferefund.gate.operations import execute_agent_action
from saferefund.policy.verdicts import Allow, RequireApproval
from saferefund.projections.case import project_case_summary
from saferefund.repositories.events import append_canonical_event, load_case_events
from saferefund.repositories.refunds import find_refund_by_id
from saferefund.repositories.seed import (
    ORD_1001_ID,
    ORD_1003_ID,
    SOPHIE_CUSTOMER_ID,
)


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
def gate_refund_session_factory(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    reset_adapters_for_tests()
    return seeded_session_factory


async def test_small_refund_auto_approves_executes_and_records_events(
    gate_refund_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with gate_refund_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_sophie",
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-refund-small",
        )
        await _link_order(session, case_id="case_sophie", order_id=ORD_1001_ID)
        verdict = await execute_agent_action(
            session,
            "case_sophie",
            ProposeRefund(action="propose_refund", amount=Decimal("249.00")),
        )

    assert verdict == Allow()
    assert len(payment.calls) == 1
    assert payment.calls[0].idempotency_key == payment.calls[0].idempotency_key
    assert payment.calls[0].amount == Decimal("249.00")

    async with gate_refund_session_factory() as session:
        case_events = await load_case_events(session, "case_sophie")
        event_types = [event.event_type for event in case_events]
        refund_events = event_types[event_types.index(EventType.ORDER_LINKED) + 1 :]
        assert refund_events == [
            EventType.REFUND_PROPOSED,
            EventType.REFUND_AUTO_APPROVED,
            EventType.REFUND_EXECUTED,
        ]

        proposed = RefundProposedPayload.model_validate(
            case_events[-3].payload,
        )
        refund_row = await find_refund_by_id(session, proposed.refund_id)
        assert refund_row is not None
        assert refund_row.status is RefundStatus.EXECUTED
        assert refund_row.amount == Decimal("249.00")

        executed = RefundExecutedPayload.model_validate(case_events[-1].payload)
        assert executed.refund_id == proposed.refund_id
        assert executed.amount == Decimal("249.00")
        assert payment.calls[0].idempotency_key == proposed.refund_id


async def test_large_refund_requires_approval_without_payment(
    gate_refund_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with gate_refund_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_sophie",
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-refund-large",
        )
        await _link_order(session, case_id="case_sophie", order_id=ORD_1003_ID)
        verdict = await execute_agent_action(
            session,
            "case_sophie",
            ProposeRefund(action="propose_refund", amount=Decimal("780.00")),
        )

    assert isinstance(verdict, RequireApproval)
    assert verdict.rule == "R_THRESHOLD"
    assert len(payment.calls) == 0

    async with gate_refund_session_factory() as session:
        case_events = await load_case_events(session, "case_sophie")
        event_types = [event.event_type for event in case_events]
        refund_events = event_types[event_types.index(EventType.ORDER_LINKED) + 1 :]
        assert refund_events == [
            EventType.REFUND_PROPOSED,
            EventType.REFUND_APPROVAL_REQUIRED,
        ]

        approval_required = RefundApprovalRequiredPayload.model_validate(
            case_events[-1].payload,
        )
        assert approval_required.amount == Decimal("780.00")
        assert approval_required.rule == "R_THRESHOLD"

        refund_row = await find_refund_by_id(session, approval_required.refund_id)
        assert refund_row is not None
        assert refund_row.status is RefundStatus.PENDING_APPROVAL
        assert refund_row.approval_expires_at is not None

        case_summary = project_case_summary(
            case_id="case_sophie",
            customer_id=SOPHIE_CUSTOMER_ID,
            events=case_events,
            now=datetime(2030, 1, 15, 9, 30, tzinfo=UTC),
        )
        assert case_summary.status is CaseStatus.AWAITING_APPROVAL
        assert case_summary.last_refund_status is RefundStatus.PENDING_APPROVAL


async def test_cumulative_threshold_second_refund_requires_approval(
    gate_refund_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with gate_refund_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_sophie",
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-cumulative",
        )
        await _link_order(session, case_id="case_sophie", order_id=ORD_1003_ID)

    async with gate_refund_session_factory.begin() as session:
        first_verdict = await execute_agent_action(
            session,
            "case_sophie",
            ProposeRefund(action="propose_refund", amount=Decimal("300.00")),
        )

    async with gate_refund_session_factory.begin() as session:
        second_verdict = await execute_agent_action(
            session,
            "case_sophie",
            ProposeRefund(action="propose_refund", amount=Decimal("300.00")),
        )

    assert first_verdict == Allow()
    assert isinstance(second_verdict, RequireApproval)
    assert second_verdict.rule == "R_THRESHOLD"
    assert len(payment.calls) == 1
    assert payment.calls[0].amount == Decimal("300.00")

    async with gate_refund_session_factory() as session:
        case_events = await load_case_events(session, "case_sophie")
        event_types = [event.event_type for event in case_events]
        assert EventType.REFUND_AUTO_APPROVED in event_types
        assert EventType.REFUND_EXECUTED in event_types
        assert event_types.count(EventType.REFUND_APPROVAL_REQUIRED) == 1

        auto_approved = next(
            event
            for event in case_events
            if event.event_type is EventType.REFUND_AUTO_APPROVED
        )
        RefundAutoApprovedPayload.model_validate(auto_approved.payload)

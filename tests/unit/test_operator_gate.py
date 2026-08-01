"""Unit tests for operator approve and reject gate operations."""

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
    RefundApprovedPayload,
    RefundExecutedPayload,
    RefundRejectedPayload,
)
from saferefund.domain.tables import CaseRow
from saferefund.gate.operations import (
    approve_refund,
    execute_agent_action,
    reject_refund,
)
from saferefund.gate.outcomes import OperatorResultKind
from saferefund.policy.verdicts import RequireApproval
from saferefund.projections.case import project_case_summary
from saferefund.repositories.events import append_canonical_event, load_case_events
from saferefund.repositories.refunds import find_refund_by_id
from saferefund.repositories.seed import ORD_1003_ID, SOPHIE_CUSTOMER_ID


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


async def _propose_large_refund(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    case_id: str,
    customer_id: str,
    opening_message_id: str,
) -> str:
    async with session_factory.begin() as session:
        await _open_case(
            session,
            case_id=case_id,
            customer_id=customer_id,
            opening_message_id=opening_message_id,
        )
        await _link_order(session, case_id=case_id, order_id=ORD_1003_ID)
        verdict = await execute_agent_action(
            session,
            case_id,
            ProposeRefund(action="propose_refund", amount=Decimal("780.00")),
        )
    assert isinstance(verdict, RequireApproval)

    async with session_factory() as session:
        case_events = await load_case_events(session, case_id)
        approval_required = RefundApprovalRequiredPayload.model_validate(
            case_events[-1].payload,
        )
        return approval_required.refund_id


@pytest.fixture
def operator_gate_session_factory(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    reset_adapters_for_tests()
    return seeded_session_factory


async def test_approve_pending_refund_executes_payment_and_records_events(
    operator_gate_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    refund_id = await _propose_large_refund(
        operator_gate_session_factory,
        case_id="case_sophie",
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-operator-approve",
    )

    async with operator_gate_session_factory.begin() as session:
        outcome = await approve_refund(
            session,
            refund_id,
            "op-1",
            session_factory=operator_gate_session_factory,
        )

    assert outcome.kind is OperatorResultKind.APPROVED
    assert outcome.case_id == "case_sophie"
    assert outcome.refund_id == refund_id
    assert outcome.refund_status is RefundStatus.EXECUTED
    assert len(payment.calls) == 1
    assert payment.calls[0].idempotency_key == refund_id
    assert payment.calls[0].amount == Decimal("780.00")

    async with operator_gate_session_factory() as session:
        case_events = await load_case_events(session, "case_sophie")
        event_types = [event.event_type for event in case_events]
        assert event_types[-2:] == [
            EventType.REFUND_APPROVED,
            EventType.REFUND_EXECUTED,
        ]

        approved = RefundApprovedPayload.model_validate(case_events[-2].payload)
        assert approved.refund_id == refund_id
        assert approved.operator_id == "op-1"
        assert case_events[-2].actor is Actor.OPERATOR
        assert case_events[-2].channel is Channel.OPERATOR_API

        executed = RefundExecutedPayload.model_validate(case_events[-1].payload)
        assert executed.refund_id == refund_id
        assert executed.amount == Decimal("780.00")

        refund_row = await find_refund_by_id(session, refund_id)
        assert refund_row is not None
        assert refund_row.status is RefundStatus.EXECUTED


async def test_reject_pending_refund_resumes_case_without_payment(
    operator_gate_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    refund_id = await _propose_large_refund(
        operator_gate_session_factory,
        case_id="case_sophie_reject",
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-operator-reject",
    )

    async with operator_gate_session_factory.begin() as session:
        outcome = await reject_refund(
            session,
            refund_id,
            "op-2",
            "amount too high for policy",
        )

    assert outcome.kind is OperatorResultKind.REJECTED
    assert outcome.case_id == "case_sophie_reject"
    assert outcome.refund_id == refund_id
    assert outcome.refund_status is RefundStatus.REJECTED
    assert len(payment.calls) == 0

    async with operator_gate_session_factory() as session:
        case_events = await load_case_events(session, "case_sophie_reject")
        rejected_event = case_events[-1]
        assert rejected_event.event_type is EventType.REFUND_REJECTED
        assert rejected_event.actor is Actor.OPERATOR
        assert rejected_event.channel is Channel.OPERATOR_API

        rejected = RefundRejectedPayload.model_validate(rejected_event.payload)
        assert rejected.refund_id == refund_id
        assert rejected.operator_id == "op-2"
        assert rejected.reason == "amount too high for policy"

        refund_row = await find_refund_by_id(session, refund_id)
        assert refund_row is not None
        assert refund_row.status is RefundStatus.REJECTED
        assert refund_row.approval_expires_at is None

        case_summary = project_case_summary(
            case_id="case_sophie_reject",
            customer_id=SOPHIE_CUSTOMER_ID,
            events=case_events,
            now=datetime(2030, 1, 15, 9, 30, tzinfo=UTC),
        )
        assert case_summary.status is CaseStatus.OPEN


async def test_approve_non_pending_returns_conflict_without_approval_event(
    operator_gate_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    refund_id = await _propose_large_refund(
        operator_gate_session_factory,
        case_id="case_sophie_conflict",
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-operator-conflict",
    )

    async with operator_gate_session_factory.begin() as session:
        first_outcome = await approve_refund(
            session,
            refund_id,
            "op-3",
            session_factory=operator_gate_session_factory,
        )
    assert first_outcome.kind is OperatorResultKind.APPROVED

    async with operator_gate_session_factory.begin() as session:
        conflict_outcome = await approve_refund(
            session,
            refund_id,
            "op-4",
            session_factory=operator_gate_session_factory,
        )

    assert conflict_outcome.kind is OperatorResultKind.CONFLICT
    assert conflict_outcome.refund_status is RefundStatus.EXECUTED
    assert len(payment.calls) == 1

    async with operator_gate_session_factory() as session:
        case_events = await load_case_events(session, "case_sophie_conflict")
        approved_events = [
            event
            for event in case_events
            if event.event_type is EventType.REFUND_APPROVED
        ]
        assert len(approved_events) == 1


async def test_reject_non_pending_returns_conflict_without_rejection_event(
    operator_gate_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    refund_id = await _propose_large_refund(
        operator_gate_session_factory,
        case_id="case_sophie_reject_conflict",
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-reject-conflict",
    )

    async with operator_gate_session_factory.begin() as session:
        approved = await approve_refund(
            session,
            refund_id,
            "op-5",
            session_factory=operator_gate_session_factory,
        )
    assert approved.kind is OperatorResultKind.APPROVED

    async with operator_gate_session_factory.begin() as session:
        conflict_outcome = await reject_refund(
            session,
            refund_id,
            "op-6",
            "too late",
        )

    assert conflict_outcome.kind is OperatorResultKind.CONFLICT
    assert conflict_outcome.refund_status is RefundStatus.EXECUTED

    async with operator_gate_session_factory() as session:
        case_events = await load_case_events(session, "case_sophie_reject_conflict")
        rejected_events = [
            event
            for event in case_events
            if event.event_type is EventType.REFUND_REJECTED
        ]
        assert len(rejected_events) == 0

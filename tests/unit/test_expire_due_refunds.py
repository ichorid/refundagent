"""Unit tests for customer-scoped approval expiry housekeeping."""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund import clock, config
from saferefund.actions.models import GetOrders, LinkOrder, ProposeRefund
from saferefund.agent.loop import run_agent_loop
from saferefund.api.routes import _drain_agent_loop_queue
from saferefund.domain.enums import (
    Actor,
    CaseStatus,
    Channel,
    OrderStatus,
    RefundStatus,
)
from saferefund.domain.events import EventType
from saferefund.domain.payloads import (
    RefundApprovalRequiredPayload,
    RefundExpiredPayload,
)
from saferefund.domain.tables import CaseRow, OrderRow
from saferefund.gate.common import expire_due_refunds_for_customer
from saferefund.gate.operations import execute_agent_action
from saferefund.policy.verdicts import RequireApproval
from saferefund.projections.case import project_case_summary
from saferefund.repositories.events import append_canonical_event, load_case_events
from saferefund.repositories.refunds import find_refund_by_id
from saferefund.repositories.seed import ORD_1003_ID, SOPHIE_CUSTOMER_ID
from tests.conftest import FIXED_TEST_NOW
from tests.support.model_gateway import scripted_gateway
from tests.support.sequence_assertions import (
    GATE_PENDING_APPROVAL_SEQUENCE,
    assert_case_expired_with_agent_resume,
    assert_exact_event_type_sequence,
    assert_expired_refund_id_matches_lifecycle,
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
            created_at=FIXED_TEST_NOW,
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


PEER_ORDER_B_ID = "ORD-1004"


async def _seed_second_large_order(session: AsyncSession) -> None:
    session.add(
        OrderRow(
            id=PEER_ORDER_B_ID,
            customer_id=SOPHIE_CUSTOMER_ID,
            item="Second coffee grinder",
            total=Decimal("650.00"),
            status=OrderStatus.DELIVERED,
        )
    )


async def _propose_large_refund(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    case_id: str,
    opening_message_id: str,
    order_id: str = ORD_1003_ID,
    amount: Decimal = Decimal("780.00"),
) -> str:
    async with session_factory.begin() as session:
        if order_id == PEER_ORDER_B_ID:
            await _seed_second_large_order(session)
        await _open_case(
            session,
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id=opening_message_id,
        )
        await execute_agent_action(session, case_id, GetOrders(action="get_orders"))
        await execute_agent_action(
            session,
            case_id,
            LinkOrder(action="link_order", order_id=order_id),
        )
        verdict = await execute_agent_action(
            session,
            case_id,
            ProposeRefund(action="propose_refund", amount=amount),
        )
    assert isinstance(verdict, RequireApproval)

    async with session_factory() as session:
        case_events = await load_case_events(session, case_id)
        approval_required = RefundApprovalRequiredPayload.model_validate(
            case_events[-1].payload,
        )
        return approval_required.refund_id


async def test_expire_due_refunds_for_customer_transitions_only_past_ttl_rows(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    refund_id = await _propose_large_refund(
        seeded_session_factory,
        case_id="case_expire_due",
        opening_message_id="msg-expire-due",
    )

    clock.set_now_for_tests(
        FIXED_TEST_NOW + timedelta(seconds=config.APPROVAL_TTL_SECONDS + 1),
    )

    async with seeded_session_factory.begin() as session:
        reopened_case_ids = await expire_due_refunds_for_customer(
            session,
            customer_id=SOPHIE_CUSTOMER_ID,
        )

    assert reopened_case_ids == ("case_expire_due",)

    async with seeded_session_factory() as session:
        case_events = await load_case_events(session, "case_expire_due")
        assert_exact_event_type_sequence(
            case_events,
            [*GATE_PENDING_APPROVAL_SEQUENCE, EventType.REFUND_EXPIRED],
        )
        assert_expired_refund_id_matches_lifecycle(case_events, refund_id=refund_id)
        expired_event = case_events[-1]
        assert expired_event.event_type is EventType.REFUND_EXPIRED
        assert expired_event.actor is Actor.SYSTEM
        assert expired_event.channel is Channel.INTERNAL

        expired_payload = RefundExpiredPayload.model_validate(expired_event.payload)
        assert expired_payload.refund_id == refund_id

        refund_row = await find_refund_by_id(session, refund_id)
        assert refund_row is not None
        assert refund_row.status is RefundStatus.EXPIRED
        assert refund_row.approval_expires_at is None

        case_summary = project_case_summary(
            case_id="case_expire_due",
            customer_id=SOPHIE_CUSTOMER_ID,
            events=case_events,
            now=clock.now(),
        )
        assert case_summary.status is CaseStatus.OPEN


async def test_expire_due_refunds_for_customer_leaves_active_pending_unchanged(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    refund_id = await _propose_large_refund(
        seeded_session_factory,
        case_id="case_expire_active",
        opening_message_id="msg-expire-active",
    )

    async with seeded_session_factory.begin() as session:
        reopened_case_ids = await expire_due_refunds_for_customer(
            session,
            customer_id=SOPHIE_CUSTOMER_ID,
        )

    assert reopened_case_ids == ()

    async with seeded_session_factory() as session:
        case_events = await load_case_events(session, "case_expire_active")
        assert not any(
            event.event_type is EventType.REFUND_EXPIRED for event in case_events
        )

        refund_row = await find_refund_by_id(session, refund_id)
        assert refund_row is not None
        assert refund_row.status is RefundStatus.PENDING_APPROVAL
        assert refund_row.approval_expires_at is not None


async def test_run_agent_loop_returns_peer_ids_without_session_factory(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mutation: absent session_factory must still return peer ids for resume."""
    await _propose_large_refund(
        seeded_session_factory,
        case_id="case_loop_return_a",
        opening_message_id="msg-loop-return-a",
    )
    await _propose_large_refund(
        seeded_session_factory,
        case_id="case_loop_return_b",
        opening_message_id="msg-loop-return-b",
        order_id=PEER_ORDER_B_ID,
        amount=Decimal("650.00"),
    )
    clock.set_now_for_tests(
        FIXED_TEST_NOW + timedelta(seconds=config.APPROVAL_TTL_SECONDS + 1),
    )

    async with seeded_session_factory.begin() as session:
        reopened_peer_ids = await run_agent_loop(
            session,
            "case_loop_return_b",
            scripted_gateway(['{"action": "finish", "summary": "Current case only."}']),
        )

    assert reopened_peer_ids == ("case_loop_return_a",)


async def test_drain_agent_loop_queue_drives_every_returned_peer_case(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mutation: in-loop peer resume deadlocks; the route queue must drain peers."""
    await _propose_large_refund(
        seeded_session_factory,
        case_id="case_loop_peer_a",
        opening_message_id="msg-loop-peer-a",
    )
    await _propose_large_refund(
        seeded_session_factory,
        case_id="case_loop_peer_b",
        opening_message_id="msg-loop-peer-b",
        order_id=PEER_ORDER_B_ID,
        amount=Decimal("650.00"),
    )
    clock.set_now_for_tests(
        FIXED_TEST_NOW + timedelta(seconds=config.APPROVAL_TTL_SECONDS + 1),
    )

    await _drain_agent_loop_queue(
        seeded_session_factory,
        ("case_loop_peer_b",),
        scripted_gateway(
            [
                '{"action": "finish", "summary": "Peer A closed after expiry."}',
                '{"action": "finish", "summary": "Peer B closed after expiry."}',
            ]
        ),
    )

    for case_id in ("case_loop_peer_a", "case_loop_peer_b"):
        async with seeded_session_factory() as session:
            case_events = await load_case_events(session, case_id)
            approval_required_event = next(
                event
                for event in case_events
                if event.event_type is EventType.REFUND_APPROVAL_REQUIRED
            )
            approval_required = RefundApprovalRequiredPayload.model_validate(
                approval_required_event.payload,
            )
            assert_case_expired_with_agent_resume(
                case_events,
                pending_sequence=GATE_PENDING_APPROVAL_SEQUENCE,
                refund_id=approval_required.refund_id,
            )


async def test_run_agent_loop_expires_before_awaiting_approval_early_return(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    refund_id = await _propose_large_refund(
        seeded_session_factory,
        case_id="case_expire_on_resume",
        opening_message_id="msg-expire-on-resume",
    )
    clock.set_now_for_tests(
        FIXED_TEST_NOW + timedelta(seconds=config.APPROVAL_TTL_SECONDS + 1),
    )

    async with seeded_session_factory.begin() as session:
        await run_agent_loop(
            session,
            "case_expire_on_resume",
            scripted_gateway(
                ['{"action": "finish", "summary": "Expired and finished."}']
            ),
            session_factory=seeded_session_factory,
        )

    async with seeded_session_factory() as session:
        case_events = await load_case_events(session, "case_expire_on_resume")
        assert_case_expired_with_agent_resume(
            case_events,
            pending_sequence=GATE_PENDING_APPROVAL_SEQUENCE,
            refund_id=refund_id,
        )
        refund_row = await find_refund_by_id(session, refund_id)
        assert refund_row is not None
        assert refund_row.status is RefundStatus.EXPIRED

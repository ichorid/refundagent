"""Every sweep-returned case id must be resumed with agent activity."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient

from saferefund import clock, config
from saferefund.api.routes import _drain_agent_loop_queue
from saferefund.domain.enums import CaseStatus
from saferefund.domain.events import EventType
from saferefund.domain.payloads import RefundApprovalRequiredPayload
from saferefund.main import create_app
from saferefund.repositories.events import load_case_events
from saferefund.repositories.seed import ORD_1003_ID
from tests.conftest import FIXED_TEST_NOW
from tests.integration.test_approval_expiry import (
    _create_pending_large_refund,
    _load_case_context,
)
from tests.support.model_gateway import scripted_gateway
from tests.support.sequence_assertions import (
    GATE_PENDING_APPROVAL_SEQUENCE,
    INBOUND_PENDING_APPROVAL_SEQUENCE,
    assert_case_expired_with_agent_resume,
)
from tests.unit.test_expire_due_refunds import (
    PEER_ORDER_B_ID,
    _propose_large_refund,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _finish_action(summary: str) -> str:
    return f'{{"action": "finish", "summary": "{summary}"}}'


async def test_expired_operator_approve_resumes_target_before_conflict(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mutation: skip resumed_case_ids on 409 and this fails on missing CASE_CLOSED."""
    case_id, refund_id = await _create_pending_large_refund(
        api_session_factory,
        message_id="msg-c15-approve-expired",
    )
    clock.set_now_for_tests(
        FIXED_TEST_NOW + timedelta(seconds=config.APPROVAL_TTL_SECONDS + 1),
    )

    app = create_app(
        session_factory=api_session_factory,
        model_gateway=scripted_gateway([_finish_action("Resumed after expiry.")]),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/operator/approve",
            json={"refund_id": refund_id, "operator_id": "op-c15"},
        )

    assert response.status_code == 409
    case_events, case_summary = await _load_case_context(api_session_factory, case_id)
    assert_case_expired_with_agent_resume(
        case_events,
        pending_sequence=INBOUND_PENDING_APPROVAL_SEQUENCE,
        refund_id=refund_id,
    )
    assert case_summary.status is CaseStatus.CLOSED


async def test_expired_operator_reject_resumes_target_before_conflict(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mutation: skip resumed_case_ids on 409 and this fails on missing CASE_CLOSED."""
    case_id, refund_id = await _create_pending_large_refund(
        api_session_factory,
        message_id="msg-c15-reject-expired",
    )
    clock.set_now_for_tests(
        FIXED_TEST_NOW + timedelta(seconds=config.APPROVAL_TTL_SECONDS + 1),
    )

    app = create_app(
        session_factory=api_session_factory,
        model_gateway=scripted_gateway(
            [_finish_action("Resumed after reject conflict.")]
        ),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/operator/reject",
            json={
                "refund_id": refund_id,
                "operator_id": "op-c15",
                "reason": "too late",
            },
        )

    assert response.status_code == 409
    case_events, case_summary = await _load_case_context(api_session_factory, case_id)
    assert_case_expired_with_agent_resume(
        case_events,
        pending_sequence=INBOUND_PENDING_APPROVAL_SEQUENCE,
        refund_id=refund_id,
    )
    assert case_summary.status is CaseStatus.CLOSED


async def test_loop_entry_sweep_resumes_every_returned_peer_case(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mutation: dropping non-current reopened ids leaves peer A silent after expiry."""
    await _propose_large_refund(
        api_session_factory,
        case_id="case_c15_peer_a",
        opening_message_id="msg-c15-peer-a",
        order_id=ORD_1003_ID,
    )
    await _propose_large_refund(
        api_session_factory,
        case_id="case_c15_peer_b",
        opening_message_id="msg-c15-peer-b",
        order_id=PEER_ORDER_B_ID,
        amount=Decimal("650.00"),
    )
    clock.set_now_for_tests(
        FIXED_TEST_NOW + timedelta(seconds=config.APPROVAL_TTL_SECONDS + 1),
    )

    await _drain_agent_loop_queue(
        api_session_factory,
        ("case_c15_peer_b",),
        scripted_gateway(
            [
                _finish_action("Peer A resumed."),
                _finish_action("Peer B resumed."),
            ]
        ),
    )

    for case_id in ("case_c15_peer_a", "case_c15_peer_b"):
        async with api_session_factory() as session:
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

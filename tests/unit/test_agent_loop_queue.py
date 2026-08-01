"""Unit tests for the route-level agent-loop work queue."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund import clock, config
from saferefund.agent.gateway import ModelGateway  # noqa: TC001
from saferefund.agent.locks import reset_case_locks_for_tests
from saferefund.api import routes as routes_module
from saferefund.api.routes import _drain_agent_loop_queue
from saferefund.domain.events import EventType
from saferefund.domain.payloads import RefundApprovalRequiredPayload
from saferefund.repositories.events import load_case_events
from tests.conftest import FIXED_TEST_NOW
from tests.support.model_gateway import scripted_gateway
from tests.support.sequence_assertions import (
    GATE_PENDING_APPROVAL_SEQUENCE,
    assert_case_expired_with_agent_resume,
)
from tests.unit.test_expire_due_refunds import PEER_ORDER_B_ID, _propose_large_refund

if TYPE_CHECKING:
    import pytest


async def test_drain_queue_commits_before_starting_peer_loop(
    seeded_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: draining peers before commit keeps nested locks and deadlocks."""
    await _propose_large_refund(
        seeded_session_factory,
        case_id="case_queue_peer_a",
        opening_message_id="msg-queue-peer-a",
    )
    await _propose_large_refund(
        seeded_session_factory,
        case_id="case_queue_peer_b",
        opening_message_id="msg-queue-peer-b",
        order_id=PEER_ORDER_B_ID,
        amount=Decimal("650.00"),
    )
    clock.set_now_for_tests(
        FIXED_TEST_NOW + timedelta(seconds=config.APPROVAL_TTL_SECONDS + 1),
    )

    reset_case_locks_for_tests()
    timeline: list[str] = []
    real_run_agent_loop = routes_module.run_agent_loop
    real_commit = AsyncSession.commit

    async def tracking_run_agent_loop(
        session: AsyncSession,
        case_id: str,
        model: ModelGateway,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> tuple[str, ...]:
        timeline.append(f"loop_start:{case_id}")
        reopened_peer_ids = await real_run_agent_loop(
            session,
            case_id,
            model,
            session_factory=session_factory,
        )
        timeline.append(f"loop_end:{case_id}")
        return reopened_peer_ids

    async def tracking_commit(self: AsyncSession) -> None:
        timeline.append("commit")
        await real_commit(self)

    monkeypatch.setattr(routes_module, "run_agent_loop", tracking_run_agent_loop)
    monkeypatch.setattr(AsyncSession, "commit", tracking_commit)

    await _drain_agent_loop_queue(
        seeded_session_factory,
        ("case_queue_peer_b",),
        scripted_gateway(
            [
                '{"action": "finish", "summary": "Peer A closed."}',
                '{"action": "finish", "summary": "Peer B closed."}',
            ]
        ),
    )

    peer_start_index = timeline.index("loop_start:case_queue_peer_a")
    current_end_index = timeline.index("loop_end:case_queue_peer_b")
    commit_index = timeline.index("commit", current_end_index)
    assert commit_index < peer_start_index

    for case_id in ("case_queue_peer_a", "case_queue_peer_b"):
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

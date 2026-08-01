"""PostgreSQL evidence that post-intent advisory scope blocks transient R_OPEN_REFUND."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from saferefund.actions.models import ProposeRefund
from saferefund.adapters import payment, reset_adapters_for_tests
from saferefund.domain.enums import RefundStatus
from saferefund.domain.events import EventType
from saferefund.domain.payloads import RefundApprovalRequiredPayload
from saferefund.domain.tables import RefundRow
from saferefund.gate import refund as refund_gate
from saferefund.gate.operations import execute_agent_action
from saferefund.policy.verdicts import Allow, Deny, RequireApproval
from saferefund.repositories.events import load_customer_events
from saferefund.repositories.seed import ORD_1003_ID, SOPHIE_CUSTOMER_ID
from tests.postgres.support.scenario import (
    THRESHOLD_PROBE_AMOUNT,
    prepare_threshold_probe_cases,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CASE_A = "case_pg_post_intent_a"
CASE_B = "case_pg_post_intent_b"


@pytest.mark.postgres
async def test_second_proposal_cannot_see_transient_open_refund_deny_during_post_intent_window(
    postgres_seeded_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hold advisory scope after intent commit until payment completes."""
    await prepare_threshold_probe_cases(
        postgres_seeded_session_factory,
        first_case_id=CASE_A,
        second_case_id=CASE_B,
    )
    reset_adapters_for_tests()

    first_post_intent = asyncio.Event()
    release_first = asyncio.Event()
    original_commit = refund_gate.commit_refund_intent_before_payment

    async def coordinating_commit(session: AsyncSession) -> None:
        await original_commit(session)
        first_post_intent.set()
        await release_first.wait()

    monkeypatch.setattr(
        refund_gate,
        "commit_refund_intent_before_payment",
        coordinating_commit,
    )

    async def propose_first() -> object:
        async with postgres_seeded_session_factory.begin() as session:
            return await execute_agent_action(
                session,
                CASE_A,
                ProposeRefund(
                    action="propose_refund",
                    amount=THRESHOLD_PROBE_AMOUNT,
                ),
                session_factory=postgres_seeded_session_factory,
            )

    async def propose_second_when_first_post_intent() -> object:
        await first_post_intent.wait()
        async with postgres_seeded_session_factory.begin() as session:
            return await execute_agent_action(
                session,
                CASE_B,
                ProposeRefund(
                    action="propose_refund",
                    amount=THRESHOLD_PROBE_AMOUNT,
                ),
                session_factory=postgres_seeded_session_factory,
            )

    first_task = asyncio.create_task(propose_first())
    await asyncio.wait_for(first_post_intent.wait(), timeout=10.0)

    second_task = asyncio.create_task(propose_second_when_first_post_intent())
    for _ in range(50):
        if second_task.done():
            break
        await asyncio.sleep(0.02)
    assert not second_task.done(), (
        "second proposal must block on advisory lock before first releases"
    )

    release_first.set()
    first_verdict, second_verdict = await asyncio.gather(first_task, second_task)

    assert first_verdict == Allow()
    assert not isinstance(second_verdict, Deny), (
        "second proposal must not observe transient R_OPEN_REFUND during post-intent window"
    )
    assert isinstance(second_verdict, RequireApproval)
    assert second_verdict.rule == "R_THRESHOLD"

    async with postgres_seeded_session_factory() as session:
        refund_rows = (
            await session.scalars(
                select(RefundRow).where(RefundRow.order_id == ORD_1003_ID)
            )
        ).all()
        customer_events = await load_customer_events(session, SOPHIE_CUSTOMER_ID)

    assert len(refund_rows) == 2
    assert sum(1 for row in refund_rows if row.status is RefundStatus.EXECUTED) == 1
    assert (
        sum(1 for row in refund_rows if row.status is RefundStatus.PENDING_APPROVAL)
        == 1
    )
    assert len(payment.calls) == 1
    assert payment.calls[0].amount == THRESHOLD_PROBE_AMOUNT

    approval_required_events = [
        event
        for event in customer_events
        if event.event_type is EventType.REFUND_APPROVAL_REQUIRED
    ]
    assert len(approval_required_events) == 1
    approval_payload = RefundApprovalRequiredPayload.model_validate(
        approval_required_events[0].payload,
    )
    assert approval_payload.amount == THRESHOLD_PROBE_AMOUNT
    assert approval_payload.rule == "R_THRESHOLD"

"""PostgreSQL concurrency evidence for R_THRESHOLD under customer-row locking.

Evidence applies only to PostgreSQL {version} (see tests/postgres/conftest.py).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from saferefund.actions.models import ProposeRefund
from saferefund.adapters import payment, reset_adapters_for_tests
from saferefund.domain.enums import RefundStatus
from saferefund.domain.events import EventType
from saferefund.domain.payloads import RefundApprovalRequiredPayload
from saferefund.domain.tables import RefundRow
from saferefund.gate.operations import execute_agent_action
from saferefund.policy.verdicts import Allow, RequireApproval
from saferefund.repositories.events import load_customer_events
from saferefund.repositories.seed import ORD_1003_ID, SOPHIE_CUSTOMER_ID
from tests.postgres.support.coordination import (
    concurrent_start_barrier,
    run_after_in_transaction_barrier,
)
from tests.postgres.support.scenario import (
    THRESHOLD_PROBE_AMOUNT,
    prepare_threshold_probe_cases,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CASE_A = "case_pg_threshold_a"
CASE_B = "case_pg_threshold_b"


@pytest.mark.postgres
async def test_two_concurrent_300_refunds_cannot_both_auto_approve(
    postgres_seeded_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """One concurrent 300.00 auto-executes and one needs approval on PostgreSQL."""
    await prepare_threshold_probe_cases(
        postgres_seeded_session_factory,
        first_case_id=CASE_A,
        second_case_id=CASE_B,
    )
    reset_adapters_for_tests()
    start_barrier = concurrent_start_barrier(2)

    async def propose_from_isolated_session(case_id: str) -> tuple[int, object]:
        return await run_after_in_transaction_barrier(
            postgres_seeded_session_factory,
            start_barrier,
            lambda session: execute_agent_action(
                session,
                case_id,
                ProposeRefund(
                    action="propose_refund",
                    amount=THRESHOLD_PROBE_AMOUNT,
                ),
                session_factory=postgres_seeded_session_factory,
            ),
        )

    (
        (first_backend_pid, first_verdict),
        (second_backend_pid, second_verdict),
    ) = await asyncio.gather(
        propose_from_isolated_session(CASE_A),
        propose_from_isolated_session(CASE_B),
    )

    assert first_backend_pid != second_backend_pid
    assert any(verdict == Allow() for verdict in (first_verdict, second_verdict))
    assert any(
        isinstance(verdict, RequireApproval) and verdict.rule == "R_THRESHOLD"
        for verdict in (first_verdict, second_verdict)
    )

    async with postgres_seeded_session_factory() as session:
        refund_rows = (
            await session.scalars(
                select(RefundRow).where(RefundRow.order_id == ORD_1003_ID)
            )
        ).all()
        customer_events = await load_customer_events(session, SOPHIE_CUSTOMER_ID)

    auto_approved_count = sum(
        1
        for event in customer_events
        if event.event_type is EventType.REFUND_AUTO_APPROVED
    )
    approval_required_count = sum(
        1
        for event in customer_events
        if event.event_type is EventType.REFUND_APPROVAL_REQUIRED
    )
    executed_count = sum(
        1 for event in customer_events if event.event_type is EventType.REFUND_EXECUTED
    )

    assert auto_approved_count == 1
    assert approval_required_count == 1
    assert executed_count == 1
    assert len(refund_rows) == 2
    assert sum(1 for row in refund_rows if row.status is RefundStatus.EXECUTED) == 1
    assert (
        sum(1 for row in refund_rows if row.status is RefundStatus.PENDING_APPROVAL)
        == 1
    )
    assert len(payment.calls) == 1
    assert payment.calls[0].amount == Decimal("300.00")

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

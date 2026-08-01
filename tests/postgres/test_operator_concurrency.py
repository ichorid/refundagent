"""PostgreSQL concurrency evidence for one-shot operator decisions.

Evidence applies only to PostgreSQL {version} (see tests/postgres/conftest.py).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from saferefund.adapters import payment, reset_adapters_for_tests
from saferefund.domain.enums import RefundStatus
from saferefund.domain.events import EventType
from saferefund.gate.operations import approve_refund, reject_refund
from saferefund.gate.outcomes import OperatorOutcome, OperatorResultKind
from saferefund.repositories.events import load_case_events
from saferefund.repositories.refunds import find_refund_by_id
from saferefund.repositories.seed import SOPHIE_CUSTOMER_ID
from tests.invariants.scenario import propose_refund_awaiting_approval
from tests.postgres.support.coordination import (
    concurrent_start_barrier,
    run_after_in_transaction_barrier,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CASE_ID = "case_pg_operator"


@pytest.mark.postgres
async def test_concurrent_approve_and_reject_have_exactly_one_winner(
    postgres_seeded_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Concurrent approve and reject races leave one decisive outcome on PostgreSQL."""
    refund_id = await propose_refund_awaiting_approval(
        postgres_seeded_session_factory,
        case_id=CASE_ID,
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-pg-operator-race",
    )
    reset_adapters_for_tests()
    start_barrier = concurrent_start_barrier(2)

    async def approve_from_isolated_session() -> tuple[int, OperatorOutcome]:
        return await run_after_in_transaction_barrier(
            postgres_seeded_session_factory,
            start_barrier,
            lambda session: approve_refund(
                session,
                refund_id,
                "operator-approve",
                session_factory=postgres_seeded_session_factory,
            ),
        )

    async def reject_from_isolated_session() -> tuple[int, OperatorOutcome]:
        return await run_after_in_transaction_barrier(
            postgres_seeded_session_factory,
            start_barrier,
            lambda session: reject_refund(
                session,
                refund_id,
                "operator-reject",
                reason="policy decline",
            ),
        )

    (
        (approve_backend_pid, approve_outcome),
        (reject_backend_pid, reject_outcome),
    ) = await asyncio.gather(
        approve_from_isolated_session(),
        reject_from_isolated_session(),
    )

    assert approve_backend_pid != reject_backend_pid

    outcomes = {approve_outcome.kind, reject_outcome.kind}
    assert OperatorResultKind.CONFLICT in outcomes
    winning_kinds = {
        OperatorResultKind.APPROVED,
        OperatorResultKind.REJECTED,
    }
    assert len(outcomes & winning_kinds) == 1

    refund_payment_calls = [
        call for call in payment.calls if call.idempotency_key == refund_id
    ]
    assert len(refund_payment_calls) <= 1

    async with postgres_seeded_session_factory() as session:
        case_events = await load_case_events(session, CASE_ID)
        refund_row = await find_refund_by_id(session, refund_id)

    approved_events = [
        event for event in case_events if event.event_type is EventType.REFUND_APPROVED
    ]
    rejected_events = [
        event for event in case_events if event.event_type is EventType.REFUND_REJECTED
    ]
    executed_events = [
        event for event in case_events if event.event_type is EventType.REFUND_EXECUTED
    ]

    assert len(approved_events) + len(rejected_events) == 1
    assert refund_row is not None

    if refund_row.status is RefundStatus.EXECUTED:
        assert len(approved_events) == 1
        assert len(rejected_events) == 0
        assert len(executed_events) == 1
        assert len(refund_payment_calls) == 1
    else:
        assert refund_row.status is RefundStatus.REJECTED
        assert len(rejected_events) == 1
        assert len(approved_events) == 0
        assert len(executed_events) == 0
        assert len(refund_payment_calls) == 0

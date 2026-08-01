"""Regression tests for peer-resume lock ordering."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from saferefund import clock, config
from saferefund.agent import loop as loop_module
from saferefund.agent.locks import reset_case_locks_for_tests
from saferefund.agent.loop import run_agent_loop
from tests.conftest import FIXED_TEST_NOW
from tests.support.model_gateway import scripted_gateway
from tests.unit.test_expire_due_refunds import (
    PEER_ORDER_B_ID,
    _propose_large_refund,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture(autouse=True)
def _reset_locks() -> None:
    reset_case_locks_for_tests()


async def test_concurrent_loops_do_not_deadlock_on_cross_case_sweep(
    seeded_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: nested peer resume at 118aed3 deadlocks when A/B sweep each other."""
    case_a = "case_deadlock_a"
    case_b = "case_deadlock_b"
    await _propose_large_refund(
        seeded_session_factory,
        case_id=case_a,
        opening_message_id="msg-deadlock-a",
    )
    await _propose_large_refund(
        seeded_session_factory,
        case_id=case_b,
        opening_message_id="msg-deadlock-b",
        order_id=PEER_ORDER_B_ID,
        amount=Decimal("650.00"),
    )
    clock.set_now_for_tests(
        FIXED_TEST_NOW + timedelta(seconds=config.APPROVAL_TTL_SECONDS + 1),
    )

    original_sweep = loop_module.expire_due_refunds_for_customer

    async def cross_case_sweep(
        session: AsyncSession,
        *,
        customer_id: str,
    ) -> tuple[str, ...]:
        await original_sweep(session, customer_id=customer_id)
        return tuple(sorted((case_a, case_b)))

    monkeypatch.setattr(
        loop_module,
        "expire_due_refunds_for_customer",
        cross_case_sweep,
    )

    finish_action = '{"action": "finish", "summary": "No deadlock."}'

    async def run_case(case_id: str) -> tuple[str, ...]:
        async with seeded_session_factory.begin() as session:
            return await run_agent_loop(
                session,
                case_id,
                scripted_gateway([finish_action]),
                session_factory=seeded_session_factory,
            )

    results = await asyncio.wait_for(
        asyncio.gather(run_case(case_a), run_case(case_b)),
        timeout=0.25,
    )

    assert set(results[0]) == {case_b}
    assert set(results[1]) == {case_a}

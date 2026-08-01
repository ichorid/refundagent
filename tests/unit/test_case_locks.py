"""Unit tests for per-case asyncio lock serialisation."""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund.agent.locks import case_execution_lock, reset_case_locks_for_tests
from saferefund.agent.loop import run_agent_loop
from saferefund.domain.enums import Actor, Channel
from saferefund.domain.events import EventType
from saferefund.domain.tables import CaseRow
from saferefund.repositories.events import append_canonical_event
from saferefund.repositories.seed import SOPHIE_CUSTOMER_ID
from tests.support.model_gateway import gateway_from_prompt_handler


class _ConcurrentProbeModel:
    """Model that yields control so two loops can race without the case lock."""

    active_runs = 0
    peak_active_runs = 0

    def __init__(self, raw_output: str) -> None:
        self._raw_output = raw_output

    async def propose(self, _prompt: object) -> str:
        _ConcurrentProbeModel.active_runs += 1
        _ConcurrentProbeModel.peak_active_runs = max(
            _ConcurrentProbeModel.peak_active_runs,
            _ConcurrentProbeModel.active_runs,
        )
        await asyncio.sleep(0.02)
        _ConcurrentProbeModel.active_runs -= 1
        return self._raw_output


@pytest.fixture(autouse=True)
def _reset_lock_registry() -> None:
    reset_case_locks_for_tests()
    _ConcurrentProbeModel.active_runs = 0
    _ConcurrentProbeModel.peak_active_runs = 0


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


async def test_case_execution_lock_serialises_same_case_id() -> None:
    """Two tasks contending for one case lock cannot hold it concurrently."""
    concurrent_holders = 0
    peak_concurrent_holders = 0
    holder_started = asyncio.Event()
    release_holder = asyncio.Event()

    async def hold_until_released() -> None:
        nonlocal concurrent_holders, peak_concurrent_holders
        async with case_execution_lock("case_a"):
            concurrent_holders += 1
            peak_concurrent_holders = max(
                peak_concurrent_holders,
                concurrent_holders,
            )
            holder_started.set()
            await release_holder.wait()
            concurrent_holders -= 1

    async def wait_for_lock() -> None:
        nonlocal concurrent_holders, peak_concurrent_holders
        await holder_started.wait()
        async with case_execution_lock("case_a"):
            concurrent_holders += 1
            peak_concurrent_holders = max(
                peak_concurrent_holders,
                concurrent_holders,
            )
            concurrent_holders -= 1

    holder_task = asyncio.create_task(hold_until_released())
    await holder_started.wait()
    waiter_task = asyncio.create_task(wait_for_lock())
    await asyncio.sleep(0.02)
    assert concurrent_holders == 1
    release_holder.set()
    await asyncio.gather(holder_task, waiter_task)
    assert peak_concurrent_holders == 1


async def test_different_case_ids_use_independent_locks() -> None:
    async with case_execution_lock("case_a"), case_execution_lock("case_b"):
        assert True


async def test_concurrent_loop_runs_for_one_case_do_not_interleave(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    probe = _ConcurrentProbeModel('{"action": "get_orders"}')
    model_gateway = gateway_from_prompt_handler(probe.propose)

    async with seeded_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_lock_probe",
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-lock-probe",
        )

    async def run_once() -> None:
        async with seeded_session_factory.begin() as session:
            await run_agent_loop(session, "case_lock_probe", model_gateway)

    await asyncio.gather(run_once(), run_once())

    assert _ConcurrentProbeModel.peak_active_runs == 1

"""Untrusted model failures and stalls terminate the case within bounded time.

The agent loop routes gateway exceptions and ``MODEL_CALL_TIMEOUT_SECONDS``
timeouts through ``invoke_model_boundary``, closing the case instead of leaving
it open indefinitely.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from saferefund import config
from saferefund.agent.loop import run_agent_loop
from saferefund.domain.enums import CaseStatus
from saferefund.domain.events import EventType
from saferefund.projections.case import project_case_summary
from saferefund.repositories.events import load_case_events, load_customer_events
from saferefund.repositories.seed import SOPHIE_CUSTOMER_ID
from tests.conftest import FIXED_TEST_NOW
from tests.invariants.scenario import open_case_row
from tests.support.model_gateway import failing_gateway, stalled_gateway

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from saferefund.agent.prompt import Prompt

STALLED_MODEL_TIMEOUT_SECONDS = 0.2
LOOP_MUST_RETURN_WITHIN_SECONDS = 5.0


class FailingModel:
    """A model backend whose call fails the way a real one eventually will."""

    async def propose(self, prompt: Prompt) -> str:
        """Raise the way an upstream client error surfaces to the loop."""
        del prompt
        message = "upstream model call failed"
        raise RuntimeError(message)


class StalledModel:
    """A model backend whose call never returns."""

    async def propose(self, prompt: Prompt) -> str:
        """Await forever, as a hung connection to a model provider does."""
        del prompt
        await asyncio.Event().wait()
        raise AssertionError


async def _case_status(
    session_factory: async_sessionmaker[AsyncSession],
    case_id: str,
) -> CaseStatus:
    async with session_factory() as session:
        customer_events = await load_customer_events(session, SOPHIE_CUSTOMER_ID)
    return project_case_summary(
        case_id=case_id,
        customer_id=SOPHIE_CUSTOMER_ID,
        events=customer_events,
        now=FIXED_TEST_NOW,
    ).status


async def test_failing_model_call_closes_the_case_instead_of_propagating(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A model failure must terminate the case, not abandon it mid-flight."""
    case_id = "case_model_failure"

    async with seeded_session_factory.begin() as session:
        await open_case_row(
            session,
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-model-failure",
        )
        await run_agent_loop(session, case_id, failing_gateway())

    assert await _case_status(seeded_session_factory, case_id) is CaseStatus.CLOSED

    async with seeded_session_factory() as session:
        case_events = await load_case_events(session, case_id)
    assert case_events[-1].event_type is EventType.CASE_CLOSED
    assert case_events[-2].event_type is EventType.ESCALATED


async def test_stalled_model_call_is_bounded_by_a_wall_clock_timeout(
    seeded_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model call that never returns must not hold the case open forever."""
    monkeypatch.setattr(
        config,
        "MODEL_CALL_TIMEOUT_SECONDS",
        STALLED_MODEL_TIMEOUT_SECONDS,
        raising=False,
    )
    case_id = "case_model_stall"

    async with seeded_session_factory.begin() as session:
        await open_case_row(
            session,
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-model-stall",
        )
        await asyncio.wait_for(
            run_agent_loop(session, case_id, stalled_gateway()),
            timeout=LOOP_MUST_RETURN_WITHIN_SECONDS,
        )

    assert await _case_status(seeded_session_factory, case_id) is CaseStatus.CLOSED


def test_model_call_timeout_is_a_declared_loop_constant() -> None:
    """The bound must be a named configuration constant, not a literal."""
    timeout_seconds = getattr(config, "MODEL_CALL_TIMEOUT_SECONDS", None)
    assert timeout_seconds is not None
    assert timeout_seconds > 0

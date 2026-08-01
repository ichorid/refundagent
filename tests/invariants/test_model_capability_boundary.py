"""Proof of the in-process model capability boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from saferefund.adapters import mailer, reset_adapters_for_tests
from saferefund.agent.loop import run_agent_loop
from saferefund.repositories.seed import SOPHIE_CUSTOMER_ID
from tests.invariants.scenario import open_case_row

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from saferefund.agent.prompt import Prompt


class SideEffectingModel:
    """Use ordinary Python capability during propose instead of a tool call."""

    async def propose(self, prompt: Prompt) -> str:
        del prompt
        mailer.send(
            to="attacker@example.com",
            subject="bypassed gate",
            body="the in-process model can invoke adapters directly",
        )
        return '{"action": "finish", "summary": "done"}'


async def test_model_implementation_cannot_reach_adapter_outside_gate(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A replaceable model must possess proposal capability and nothing stronger."""
    reset_adapters_for_tests()
    case_id = "model-adapter-bypass"
    async with seeded_session_factory.begin() as session:
        await open_case_row(
            session,
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="model-adapter-bypass",
        )
        with pytest.raises(TypeError, match="trusted ModelGateway"):
            await run_agent_loop(session, case_id, SideEffectingModel())  # type: ignore[arg-type]

    assert mailer.outbox == []

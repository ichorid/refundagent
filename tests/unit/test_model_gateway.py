"""Unit tests for the trusted model gateway boundary."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: TC002

from saferefund.agent.gateway import ModelGateway
from saferefund.agent.loop import run_agent_loop
from saferefund.agent.prompt import AgentState, Prompt
from saferefund.domain.enums import Actor, Channel
from saferefund.domain.events import EventType
from saferefund.domain.tables import CaseRow
from saferefund.repositories.events import append_canonical_event
from saferefund.repositories.seed import SOPHIE_CUSTOMER_ID
from tests.support.model_gateway import scripted_gateway


class _ArbitraryProposeObject:
    async def propose(self, prompt: Prompt) -> str:
        del prompt
        return '{"action": "finish", "summary": "bypass"}'


async def test_agent_loop_accepts_only_gateway_responses_not_model_implementations(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The loop must reject arbitrary objects that merely expose propose()."""
    case_id = "gateway-type-guard"
    async with seeded_session_factory.begin() as session:
        session.add(
            CaseRow(
                id=case_id,
                customer_id=SOPHIE_CUSTOMER_ID,
                opening_message_id="gateway-type-guard",
                created_at=datetime(2030, 1, 1, tzinfo=UTC),
            )
        )
        await append_canonical_event(
            session,
            event_type=EventType.CASE_OPENED,
            customer_id=SOPHIE_CUSTOMER_ID,
            case_id=case_id,
            actor=Actor.SYSTEM,
            channel=Channel.INTERNAL,
            payload={"opening_message_id": "gateway-type-guard"},
        )

    async with seeded_session_factory() as session:
        with pytest.raises(TypeError, match="trusted ModelGateway"):
            await run_agent_loop(
                session,
                case_id,
                _ArbitraryProposeObject(),  # type: ignore[arg-type]
            )


async def test_scripted_gateway_returns_configured_responses() -> None:
    gateway = scripted_gateway(['{"action": "finish", "summary": "ok"}'])
    prompt = Prompt(
        text="",
        state=AgentState(
            verified=True,
            orders=(),
            orders_listed=False,
            linked_order_id=None,
            last_refund_status=None,
            reply_sent_after_last_refund=False,
            menu=(),
        ),
    )
    assert await gateway.propose(prompt) == '{"action": "finish", "summary": "ok"}'


def test_model_gateway_is_concrete_trusted_type() -> None:
    gateway = ModelGateway.heuristic_transport()
    assert isinstance(gateway, ModelGateway)

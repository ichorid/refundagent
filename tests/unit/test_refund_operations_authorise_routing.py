"""Behavioral evidence that ProposeRefund never uses operations.authorise pre-lock."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from saferefund import config
from saferefund.actions.models import Action, ProposeRefund
from saferefund.adapters import payment, reset_adapters_for_tests, ticketing
from saferefund.domain.enums import Actor, CaseOutcome, Channel
from saferefund.domain.events import EventType
from saferefund.domain.payloads import (
    ActionDeniedPayload,
    CaseClosedPayload,
    EscalatedPayload,
)
from saferefund.domain.tables import CaseRow
from saferefund.gate import operations
from saferefund.gate.operations import execute_agent_action
from saferefund.policy.authorisation import Authorisation  # noqa: TC001
from saferefund.policy.context import RuleContext  # noqa: TC001
from saferefund.policy.verdicts import Deny, ForceEscalate, RequireApproval
from saferefund.repositories.events import append_canonical_event, load_case_events
from saferefund.repositories.seed import SOPHIE_CUSTOMER_ID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture
def track_operations_authorise(
    monkeypatch: pytest.MonkeyPatch,
) -> list[Action]:
    """Record every action passed to the gate operations authorise wrapper."""
    calls: list[Action] = []
    original_authorise = operations.authorise

    def recording_authorise(
        ctx: RuleContext,
        action: Action,
    ) -> Authorisation | Deny | RequireApproval | ForceEscalate:
        calls.append(action)
        return original_authorise(ctx, action)

    monkeypatch.setattr(operations, "authorise", recording_authorise)
    return calls


@pytest.fixture
def refund_routing_session_factory(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    reset_adapters_for_tests()
    return seeded_session_factory


async def _open_case(session: AsyncSession, *, case_id: str) -> None:
    session.add(
        CaseRow(
            id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id=f"msg-{case_id}",
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
        payload={"opening_message_id": f"msg-{case_id}"},
    )


def _assert_no_propose_refund_operations_authorise(
    calls: list[object],
    *,
    label: str,
) -> None:
    propose_refund_calls = [
        action for action in calls if isinstance(action, ProposeRefund)
    ]
    assert propose_refund_calls == [], (
        f"{label}: operations.authorise must not run for ProposeRefund; "
        f"saw {len(propose_refund_calls)} call(s)"
    )


async def test_propose_refund_denial_skips_operations_authorise(
    refund_routing_session_factory: async_sessionmaker[AsyncSession],
    track_operations_authorise: list[object],
) -> None:
    """Locked refund denial must not consult operations.authorise pre-lock."""
    async with refund_routing_session_factory.begin() as session:
        await _open_case(session, case_id="case_refund_routing_deny")
        verdict = await execute_agent_action(
            session,
            "case_refund_routing_deny",
            ProposeRefund(action="propose_refund", amount=Decimal("10.00")),
        )

    assert isinstance(verdict, Deny)
    assert verdict.rule == "R_NO_LINKED_ORDER"
    _assert_no_propose_refund_operations_authorise(
        track_operations_authorise,
        label="denial path",
    )

    async with refund_routing_session_factory() as session:
        case_events = await load_case_events(session, "case_refund_routing_deny")
        denied = next(
            event
            for event in case_events
            if event.event_type is EventType.ACTION_DENIED
        )
        denied_payload = ActionDeniedPayload.model_validate(denied.payload)
        assert denied_payload.rule == "R_NO_LINKED_ORDER"


async def test_propose_refund_force_escalate_skips_operations_authorise(
    refund_routing_session_factory: async_sessionmaker[AsyncSession],
    track_operations_authorise: list[object],
) -> None:
    """Locked refund ForceEscalate must not consult operations.authorise pre-lock."""
    case_id = "case_refund_routing_escalate"
    refund_action = ProposeRefund(action="propose_refund", amount=Decimal("10.00"))

    async with refund_routing_session_factory.begin() as session:
        await _open_case(session, case_id=case_id)
        for _ in range(config.DENIAL_LOOP_THRESHOLD):
            denial = await execute_agent_action(session, case_id, refund_action)
            assert isinstance(denial, Deny)
        verdict = await execute_agent_action(session, case_id, refund_action)

    assert isinstance(verdict, ForceEscalate)
    assert verdict.rule == "R_DENIAL_LOOP"
    _assert_no_propose_refund_operations_authorise(
        track_operations_authorise,
        label="force-escalate path",
    )

    async with refund_routing_session_factory() as session:
        case_events = await load_case_events(session, case_id)
        assert case_events[-2].event_type is EventType.ESCALATED
        escalated_payload = EscalatedPayload.model_validate(case_events[-2].payload)
        assert escalated_payload.origin.value == "policy"
        assert case_events[-1].event_type is EventType.CASE_CLOSED
        closed_payload = CaseClosedPayload.model_validate(case_events[-1].payload)
        assert closed_payload.outcome is CaseOutcome.ESCALATED
        assert len(ticketing.escalations) == 1
        assert len(payment.calls) == 0

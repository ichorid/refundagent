"""Loop termination follows projected CaseStatus, not parsed action type.

Mutation that turns this red: restore `_terminates_after_agent_action` so a denied
`Finish` returns immediately while the case remains open.
"""

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund.actions.models import Action, Finish
from saferefund.adapters import ticketing
from saferefund.agent.loop import run_agent_loop
from saferefund.domain.enums import (
    Actor,
    CaseOutcome,
    CaseStatus,
    Channel,
    EscalationOrigin,
)
from saferefund.domain.events import EventType
from saferefund.domain.payloads import (
    ActionDeniedPayload,
    CaseClosedPayload,
    EscalatedPayload,
)
from saferefund.domain.tables import CaseRow
from saferefund.gate import operations
from saferefund.policy.authorisation import Authorisation
from saferefund.policy.context import RuleContext
from saferefund.policy.verdicts import Deny, ForceEscalate, RequireApproval
from saferefund.projections.case import project_case_summary
from saferefund.repositories.events import (
    append_canonical_event,
    load_case_events,
    load_customer_events,
)
from saferefund.repositories.seed import SOPHIE_CUSTOMER_ID
from tests.support.model_gateway import scripted_gateway

_FINISH_DENY = Deny(
    rule="R_TEST_FINISH_DENIED",
    agent_reason="Finish is denied for this proof.",
    customer_reason="Finish is denied for this proof.",
)


def _authorise_denying_finish_after_universal_checks(
    ctx: RuleContext,
    action: Action,
    *,
    _original_authorise: Callable[
        [RuleContext, Action],
        Authorisation | Deny | RequireApproval | ForceEscalate,
    ],
) -> Authorisation | Deny | RequireApproval | ForceEscalate:
    result = _original_authorise(ctx, action)
    if isinstance(action, Finish) and isinstance(result, Authorisation):
        return _FINISH_DENY
    return result


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


async def test_denied_finish_closes_via_denial_loop_not_action_type(
    api_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: restore action-type termination; denied Finish stays open."""
    original_authorise = operations.authorise
    monkeypatch.setattr(
        operations,
        "authorise",
        lambda ctx, action: _authorise_denying_finish_after_universal_checks(
            ctx,
            action,
            _original_authorise=original_authorise,
        ),
    )

    case_id = "case_f5_denied_finish"
    model = scripted_gateway(
        ['{"action": "finish", "summary": "done"}'] * 4,
    )

    async with api_session_factory.begin() as session:
        await _open_case(
            session,
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-f5-denied-finish",
        )
        await run_agent_loop(session, case_id, model)

    async with api_session_factory() as session:
        case_events = await load_case_events(session, case_id)
        customer_events = await load_customer_events(session, SOPHIE_CUSTOMER_ID)
        case_summary = project_case_summary(
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            events=customer_events,
            now=datetime(2030, 1, 15, 9, 30, tzinfo=UTC),
        )

    assert case_summary.status is CaseStatus.CLOSED

    denied_events = [
        event for event in case_events if event.event_type is EventType.ACTION_DENIED
    ]
    assert len(denied_events) == 3
    for event in denied_events:
        denied = ActionDeniedPayload.model_validate(event.payload)
        assert denied.rule == "R_TEST_FINISH_DENIED"

    escalated = next(
        event for event in case_events if event.event_type is EventType.ESCALATED
    )
    escalated_payload = EscalatedPayload.model_validate(escalated.payload)
    assert escalated_payload.origin is EscalationOrigin.POLICY
    assert len(ticketing.escalations) == 1

    closed = case_events[-1]
    assert closed.event_type is EventType.CASE_CLOSED
    closed_payload = CaseClosedPayload.model_validate(closed.payload)
    assert closed_payload.outcome is CaseOutcome.ESCALATED

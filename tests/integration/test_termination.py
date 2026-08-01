"""Integration scenarios 10-13: termination paths via HTTP and ScriptedModel."""

from typing import Any, cast

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund import config
from saferefund.agent.gateway import ModelGateway
from saferefund.domain.enums import CaseOutcome, CaseStatus, EscalationOrigin
from saferefund.domain.events import EventType
from saferefund.domain.payloads import ActionDeniedPayload, InvalidOutputPayload
from saferefund.main import create_app
from saferefund.projections.case import project_case_summary
from saferefund.repositories.events import load_case_events, load_customer_events
from saferefund.repositories.seed import SOPHIE_CUSTOMER_ID, SOPHIE_EMAIL
from tests.conftest import FIXED_TEST_NOW
from tests.support.model_gateway import scripted_gateway
from tests.support.sequence_assertions import (
    agent_escalation_termination_sequence,
    assert_terminal_escalation_closure,
    denial_loop_termination_sequence,
    parse_limit_termination_sequence,
    step_limit_termination_sequence,
)


def _action_json(action: str, **fields: str) -> str:
    parts = [f'"action": "{action}"']
    parts.extend(f'"{key}": "{value}"' for key, value in fields.items())
    return "{" + ", ".join(parts) + "}"


def _scripted_model(*actions: str) -> ModelGateway:
    return scripted_gateway(actions)


async def _post_inbound_email(
    client: AsyncClient,
    *,
    message_id: str,
    subject: str = "Refund request",
    body: str = "Please refund my order.",
) -> dict[str, Any]:
    response = await client.post(
        "/inbound-email",
        json={
            "envelope_from": SOPHIE_EMAIL,
            "message_id": message_id,
            "subject": subject,
            "body": body,
        },
    )
    assert response.status_code == 200
    return cast("dict[str, Any]", response.json())


async def _load_closed_case(
    session_factory: async_sessionmaker[AsyncSession],
    case_id: str,
) -> tuple[list[Any], Any]:
    async with session_factory() as session:
        case_events = await load_case_events(session, case_id)
        customer_events = await load_customer_events(session, SOPHIE_CUSTOMER_ID)
        case_summary = project_case_summary(
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            events=customer_events,
            now=FIXED_TEST_NOW,
        )
    assert case_events[-1].event_type is EventType.CASE_CLOSED
    assert case_summary.status is CaseStatus.CLOSED
    return case_events, case_summary


async def test_denial_loop_forces_escalation(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Scenario 10: refund proposals without a linked order force escalation."""
    script = [_action_json("propose_refund", amount="10.00")] * 4
    app = create_app(
        session_factory=api_session_factory,
        model_gateway=_scripted_model(*script),
    )
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        inbound = await _post_inbound_email(
            client,
            message_id="msg-termination-denial-loop",
        )

    assert inbound["status"] == CaseStatus.CLOSED.value
    case_id = inbound["case_id"]
    case_events, case_summary = await _load_closed_case(api_session_factory, case_id)

    assert_terminal_escalation_closure(
        case_events,
        expected_types=denial_loop_termination_sequence(),
        origin=EscalationOrigin.POLICY,
        outcome=CaseOutcome.ESCALATED,
    )
    for event in case_events:
        if event.event_type is not EventType.ACTION_DENIED:
            continue
        denied = ActionDeniedPayload.model_validate(event.payload)
        assert denied.rule == "R_NO_LINKED_ORDER"
    assert case_summary.status is CaseStatus.CLOSED


async def test_agent_escalation_closes_case(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Scenario 11: agent escalation closes the case and retries do not reopen it."""
    script = [_action_json("escalate", reason="Need a human operator.")]
    app = create_app(
        session_factory=api_session_factory,
        model_gateway=_scripted_model(*script),
    )
    transport = ASGITransport(app=app)
    message_id = "msg-termination-agent-escalate"

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await _post_inbound_email(client, message_id=message_id)
        first_event_count = len(first["events"])

        second = await client.post(
            "/inbound-email",
            json={
                "envelope_from": SOPHIE_EMAIL,
                "message_id": message_id,
                "subject": "Follow-up",
                "body": "Any update?",
            },
        )

    assert first["status"] == CaseStatus.CLOSED.value
    case_id = first["case_id"]
    case_events, case_summary = await _load_closed_case(api_session_factory, case_id)

    assert_terminal_escalation_closure(
        case_events,
        expected_types=agent_escalation_termination_sequence(),
        origin=EscalationOrigin.AGENT,
        outcome=CaseOutcome.ESCALATED,
    )

    assert second.status_code == 200
    second_body = second.json()
    assert second_body["case_id"] == case_id
    assert second_body["status"] == CaseStatus.CLOSED.value
    assert len(second_body["events"]) == first_event_count

    async with api_session_factory() as session:
        case_events_after_retry = await load_case_events(session, case_id)
    assert [event.event_type for event in case_events_after_retry] == [
        event.event_type for event in case_events
    ]
    assert case_summary.status is CaseStatus.CLOSED


async def test_step_limit(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Scenario 12: repeated allowed actions close the case at the step limit."""
    script = [_action_json("get_orders")] * (config.MAX_AGENT_STEPS + 1)
    app = create_app(
        session_factory=api_session_factory,
        model_gateway=_scripted_model(*script),
    )
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        inbound = await _post_inbound_email(
            client,
            message_id="msg-termination-step-limit",
        )

    assert inbound["status"] == CaseStatus.CLOSED.value
    case_id = inbound["case_id"]
    case_events, case_summary = await _load_closed_case(api_session_factory, case_id)

    assert_terminal_escalation_closure(
        case_events,
        expected_types=step_limit_termination_sequence(),
        origin=EscalationOrigin.STEP_LIMIT,
        outcome=CaseOutcome.STEP_LIMIT,
    )
    assert case_summary.status is CaseStatus.CLOSED


async def test_parse_failure_limit(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Scenario 13: malformed outputs close the case at the parse limit."""
    script = ["not-json", "{bad", ""]
    app = create_app(
        session_factory=api_session_factory,
        model_gateway=_scripted_model(*script),
    )
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        inbound = await _post_inbound_email(
            client,
            message_id="msg-termination-parse-limit",
        )

    assert inbound["status"] == CaseStatus.CLOSED.value
    case_id = inbound["case_id"]
    case_events, case_summary = await _load_closed_case(api_session_factory, case_id)

    assert_terminal_escalation_closure(
        case_events,
        expected_types=parse_limit_termination_sequence(),
        origin=EscalationOrigin.PARSE_LIMIT,
        outcome=CaseOutcome.PARSE_LIMIT,
    )
    for event in case_events:
        if event.event_type is not EventType.INVALID_OUTPUT:
            continue
        InvalidOutputPayload.model_validate(event.payload)
    assert case_summary.status is CaseStatus.CLOSED

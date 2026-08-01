"""Unit tests for the resumable agent loop and hard limits."""

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund import config
from saferefund.actions.models import Finish, GetOrders, LinkOrder, ProposeRefund
from saferefund.adapters import reset_adapters_for_tests, ticketing
from saferefund.agent.locks import reset_case_locks_for_tests
from saferefund.agent.loop import append_invalid_output, run_agent_loop
from saferefund.domain.enums import Actor, CaseOutcome, CaseStatus, Channel
from saferefund.domain.events import EventType
from saferefund.domain.payloads import (
    CaseClosedPayload,
    EscalatedPayload,
    InvalidOutputPayload,
)
from saferefund.domain.tables import CaseRow
from saferefund.gate.operations import execute_agent_action
from saferefund.projections.case import project_case_summary
from saferefund.repositories.events import (
    StoredEvent,
    append_canonical_event,
    load_case_events,
    load_customer_events,
)
from saferefund.repositories.seed import ORD_1003_ID, SOPHIE_CUSTOMER_ID
from tests.support.model_gateway import scripted_gateway


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


def _agent_event_count(case_events: Sequence[StoredEvent]) -> int:
    return sum(1 for event in case_events if event.actor is Actor.AGENT)


@pytest.fixture(autouse=True)
def _reset_loop_dependencies() -> None:
    reset_adapters_for_tests()
    reset_case_locks_for_tests()


@pytest.fixture
def loop_session_factory(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    return seeded_session_factory


async def test_loop_resumes_from_existing_step_count(
    loop_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    case_id = "case_resume_steps"
    resume_model = scripted_gateway(
        [
            '{"action": "get_orders"}',
            '{"action": "finish", "summary": "resumed"}',
        ]
    )

    async with loop_session_factory.begin() as session:
        await _open_case(
            session,
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-resume-steps",
        )
        await execute_agent_action(session, case_id, GetOrders(action="get_orders"))
        await execute_agent_action(session, case_id, GetOrders(action="get_orders"))

    async with loop_session_factory.begin() as session:
        await run_agent_loop(session, case_id, resume_model)

    async with loop_session_factory() as session:
        case_events = await load_case_events(session, case_id)
        assert _agent_event_count(case_events) == 3
        customer_events = await load_customer_events(session, SOPHIE_CUSTOMER_ID)
        case_summary = project_case_summary(
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            events=customer_events,
            now=datetime(2030, 1, 15, 9, 30, tzinfo=UTC),
        )
        assert case_summary.step_count == 4
        assert case_summary.status is CaseStatus.CLOSED


async def test_three_malformed_outputs_close_via_parse_limit(
    loop_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    case_id = "case_parse_limit"
    model = scripted_gateway(["not-json", "{bad", ""])

    async with loop_session_factory.begin() as session:
        await _open_case(
            session,
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-parse-limit",
        )
        await run_agent_loop(session, case_id, model)

    async with loop_session_factory() as session:
        case_events = await load_case_events(session, case_id)
        invalid_events = [
            event
            for event in case_events
            if event.event_type is EventType.INVALID_OUTPUT
        ]
        assert len(invalid_events) == 3
        for event in invalid_events:
            InvalidOutputPayload.model_validate(event.payload)

        escalated = next(
            event for event in case_events if event.event_type is EventType.ESCALATED
        )
        escalated_payload = EscalatedPayload.model_validate(escalated.payload)
        assert escalated_payload.origin.value == "parse_limit"

        closed = case_events[-1]
        assert closed.event_type is EventType.CASE_CLOSED
        closed_payload = CaseClosedPayload.model_validate(closed.payload)
        assert closed_payload.outcome is CaseOutcome.PARSE_LIMIT


async def test_denied_action_resets_parser_counter_before_parse_limit(
    loop_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    case_id = "case_denial_resets_parser"
    model = scripted_gateway(
        [
            "not-json",
            '{"action": "propose_refund", "amount": "10.00"}',
            "still-not-json",
            "also-not-json",
            '{"action": "finish", "summary": "parser counter reset"}',
        ]
    )

    async with loop_session_factory.begin() as session:
        await _open_case(
            session,
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-denial-parser",
        )
        await run_agent_loop(session, case_id, model)

    async with loop_session_factory() as session:
        case_events = await load_case_events(session, case_id)
        invalid_events = [
            event
            for event in case_events
            if event.event_type is EventType.INVALID_OUTPUT
        ]
        assert len(invalid_events) == 3
        assert not any(event.event_type is EventType.ESCALATED for event in case_events)


async def test_repeated_allowed_actions_close_via_step_limit(
    loop_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    case_id = "case_step_limit"
    model = scripted_gateway(
        ['{"action": "get_orders"}'] * (config.MAX_AGENT_STEPS + 1)
    )

    async with loop_session_factory.begin() as session:
        await _open_case(
            session,
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-step-limit",
        )
        await run_agent_loop(session, case_id, model)

    async with loop_session_factory() as session:
        case_events = await load_case_events(session, case_id)
        assert _agent_event_count(case_events) == config.MAX_AGENT_STEPS

        escalated = next(
            event for event in case_events if event.event_type is EventType.ESCALATED
        )
        escalated_payload = EscalatedPayload.model_validate(escalated.payload)
        assert escalated_payload.origin.value == "step_limit"

        closed = case_events[-1]
        assert closed.event_type is EventType.CASE_CLOSED
        closed_payload = CaseClosedPayload.model_validate(closed.payload)
        assert closed_payload.outcome is CaseOutcome.STEP_LIMIT


async def test_denial_loop_force_escalate_closes_case(
    loop_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    case_id = "case_denial_loop"
    refund_action = ProposeRefund(
        action="propose_refund",
        amount=Decimal("10.00"),
    )
    model = scripted_gateway([refund_action.model_dump_json()] * 4)

    async with loop_session_factory.begin() as session:
        await _open_case(
            session,
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-denial-loop",
        )
        await run_agent_loop(session, case_id, model)

    async with loop_session_factory() as session:
        case_events = await load_case_events(session, case_id)
        denied_events = [
            event
            for event in case_events
            if event.event_type is EventType.ACTION_DENIED
        ]
        assert len(denied_events) == 3

        escalated = next(
            event for event in case_events if event.event_type is EventType.ESCALATED
        )
        escalated_payload = EscalatedPayload.model_validate(escalated.payload)
        assert escalated_payload.origin.value == "policy"
        assert len(ticketing.escalations) == 1

        closed = case_events[-1]
        assert closed.event_type is EventType.CASE_CLOSED
        closed_payload = CaseClosedPayload.model_validate(closed.payload)
        assert closed_payload.outcome is CaseOutcome.ESCALATED


async def test_closed_case_returns_without_appending_events(
    loop_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    case_id = "case_already_closed"
    model = scripted_gateway(['{"action": "get_orders"}'])

    async with loop_session_factory.begin() as session:
        await _open_case(
            session,
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-closed",
        )
        await execute_agent_action(
            session,
            case_id,
            Finish(action="finish", summary="done"),
        )

    async with loop_session_factory.begin() as session:
        customer_events_before = await load_customer_events(
            session,
            SOPHIE_CUSTOMER_ID,
        )
        event_count_before = len(customer_events_before)
        await run_agent_loop(session, case_id, model)
        customer_events_after = await load_customer_events(
            session,
            SOPHIE_CUSTOMER_ID,
        )

    assert len(customer_events_after) == event_count_before


async def test_awaiting_approval_suspends_without_process_memory(
    loop_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    case_id = "case_suspend_approval"
    resume_model = scripted_gateway(['{"action": "get_orders"}'])

    async with loop_session_factory.begin() as session:
        await _open_case(
            session,
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-suspend-approval",
        )
        await execute_agent_action(session, case_id, GetOrders(action="get_orders"))
        await execute_agent_action(
            session,
            case_id,
            LinkOrder(action="link_order", order_id=ORD_1003_ID),
        )
        await execute_agent_action(
            session,
            case_id,
            ProposeRefund(action="propose_refund", amount=Decimal("780.00")),
        )

    async with loop_session_factory.begin() as session:
        await run_agent_loop(session, case_id, resume_model)

    async with loop_session_factory() as session:
        case_events = await load_case_events(session, case_id)
        assert _agent_event_count(case_events) == 3
        customer_events = await load_customer_events(session, SOPHIE_CUSTOMER_ID)
        case_summary = project_case_summary(
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            events=customer_events,
            now=datetime(2030, 1, 15, 9, 30, tzinfo=UTC),
        )
        assert case_summary.status is CaseStatus.AWAITING_APPROVAL


async def test_mixed_denials_and_invalid_outputs_hit_unified_step_limit(
    loop_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Denied actions and invalid outputs advance step_count toward the cap."""
    case_id = "case_mixed_step_limit"
    model = scripted_gateway(["not-json", '{"action": "get_orders"}'] * 6)

    async with loop_session_factory.begin() as session:
        await _open_case(
            session,
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-mixed-step-limit",
        )
        await run_agent_loop(session, case_id, model)

    async with loop_session_factory() as session:
        case_events = await load_case_events(session, case_id)
        customer_events = await load_customer_events(session, SOPHIE_CUSTOMER_ID)
        case_summary = project_case_summary(
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            events=customer_events,
            now=datetime(2030, 1, 15, 9, 30, tzinfo=UTC),
        )
        assert case_summary.step_count == config.MAX_AGENT_STEPS
        assert case_summary.status is CaseStatus.CLOSED

        escalated = next(
            event for event in case_events if event.event_type is EventType.ESCALATED
        )
        escalated_payload = EscalatedPayload.model_validate(escalated.payload)
        assert escalated_payload.origin.value == "step_limit"


async def test_step_limit_precedes_parse_limit_when_both_thresholds_reached(
    loop_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """MAX_AGENT_STEPS is checked before MAX_INVALID_OUTPUTS at loop entry."""
    case_id = "case_step_before_parse"
    model = scripted_gateway(
        ['{"action": "get_orders"}'] * 9 + ["not-json"] * 3,
    )

    async with loop_session_factory.begin() as session:
        await _open_case(
            session,
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-step-before-parse",
        )
        await run_agent_loop(session, case_id, model)

    async with loop_session_factory() as session:
        case_events = await load_case_events(session, case_id)
        customer_events = await load_customer_events(session, SOPHIE_CUSTOMER_ID)
        case_summary = project_case_summary(
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            events=customer_events,
            now=datetime(2030, 1, 15, 9, 30, tzinfo=UTC),
        )
        assert case_summary.step_count == config.MAX_AGENT_STEPS
        assert case_summary.consecutive_invalid_outputs == config.MAX_INVALID_OUTPUTS

        escalated = next(
            event for event in case_events if event.event_type is EventType.ESCALATED
        )
        escalated_payload = EscalatedPayload.model_validate(escalated.payload)
        assert escalated_payload.origin.value == "step_limit"


async def test_invalid_output_persists_bounded_audit_metadata(
    loop_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Within-limit malformed output stores preview bytes, count, and digest only."""
    case_id = "case_invalid_output_audit"
    max_preview = config.INVALID_OUTPUT_PREVIEW_MAX_BYTES
    prefix = "a" * (max_preview - 1)
    raw_output = prefix + "🙂" + ("z" * 100)
    raw_bytes = raw_output.encode("utf-8")
    assert len(raw_bytes) > max_preview
    assert len(prefix.encode("utf-8")) == max_preview - 1

    async with loop_session_factory.begin() as session:
        await _open_case(
            session,
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-invalid-audit",
        )
        await append_invalid_output(
            session,
            customer_id=SOPHIE_CUSTOMER_ID,
            case_id=case_id,
            raw_model_output=raw_output,
            parse_error="malformed JSON",
        )

    async with loop_session_factory() as session:
        case_events = await load_case_events(session, case_id)
    invalid_event = next(
        event for event in case_events if event.event_type is EventType.INVALID_OUTPUT
    )
    payload = InvalidOutputPayload.model_validate(invalid_event.payload)
    preview_bytes = payload.preview.encode("utf-8")
    assert len(preview_bytes) <= max_preview
    assert preview_bytes == prefix.encode("utf-8")
    assert "🙂" not in payload.preview
    preview_bytes.decode("utf-8")
    assert payload.byte_count == len(raw_bytes)
    assert payload.sha256 == hashlib.sha256(raw_bytes).hexdigest()
    assert payload.error == "malformed JSON"
    assert payload.preview != raw_output


async def test_prompt_envelope_exceeded_closes_via_model_failure(
    loop_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oversized prompt envelopes fail closed before any model invocation."""
    monkeypatch.setattr(config, "PROMPT_TOTAL_MAX_BYTES", 64)
    case_id = "case_prompt_envelope"
    model = scripted_gateway(['{"action": "finish", "summary": "should not run"}'])

    async with loop_session_factory.begin() as session:
        await _open_case(
            session,
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-prompt-envelope",
        )
        await run_agent_loop(session, case_id, model)

    async with loop_session_factory() as session:
        case_events = await load_case_events(session, case_id)
    assert [event.event_type for event in case_events] == [
        EventType.CASE_OPENED,
        EventType.ESCALATED,
        EventType.CASE_CLOSED,
    ]
    escalated_payload = EscalatedPayload.model_validate(case_events[-2].payload)
    assert escalated_payload.origin.value == "model_failure"
    assert "utf-8 byte envelope" in escalated_payload.reason.lower()


async def test_authorized_order_count_exceeds_prompt_envelope_closes_via_model_failure(
    loop_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Excessive authorized order counts fail closed before any model invocation."""
    monkeypatch.setattr(config, "AUTHORIZED_ORDER_COUNT_MAX", 1)
    case_id = "case_order_count_envelope"
    model = scripted_gateway(['{"action": "finish", "summary": "should not run"}'])

    async with loop_session_factory.begin() as session:
        await _open_case(
            session,
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-order-count-envelope",
        )
        await execute_agent_action(session, case_id, GetOrders(action="get_orders"))
        await run_agent_loop(session, case_id, model)

    async with loop_session_factory() as session:
        case_events = await load_case_events(session, case_id)
    assert [event.event_type for event in case_events] == [
        EventType.CASE_OPENED,
        EventType.ORDERS_LISTED,
        EventType.ESCALATED,
        EventType.CASE_CLOSED,
    ]
    escalated_payload = EscalatedPayload.model_validate(case_events[-2].payload)
    assert escalated_payload.origin.value == "model_failure"
    assert "order count" in escalated_payload.reason.lower()

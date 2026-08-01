"""Unit tests for gate escalation and closure outcome mapping."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund import clock, ids
from saferefund.actions.models import Escalate, SendReply
from saferefund.adapters import reset_adapters_for_tests, ticketing
from saferefund.db import (
    create_all,
    create_database_engine,
    create_session_factory,
    dispose_database,
)
from saferefund.domain.enums import (
    Actor,
    CaseOutcome,
    CaseStatus,
    Channel,
    EscalationOrigin,
)
from saferefund.domain.events import EventType
from saferefund.domain.payloads import CaseClosedPayload, EscalatedPayload
from saferefund.domain.tables import CaseRow, CustomerRow, EventRow
from saferefund.gate.common import (
    case_outcome_for_escalation_origin,
    escalated_actor_for_origin,
    load_rule_context_for_case,
)
from saferefund.gate.operations import escalate_case
from saferefund.policy.authorisation import Authorisation, AuthorisationError
from saferefund.policy.checks import applicable_obligations
from saferefund.policy.verdicts import ForceEscalate
from saferefund.projections.case import project_case_summary
from saferefund.repositories.events import append_canonical_event, load_case_events


@pytest.fixture
async def gate_escalation_session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    ids.reset_counter_for_tests()
    clock.reset_now_for_tests()
    clock.set_now_for_tests(datetime(2030, 1, 15, 9, 30, tzinfo=UTC))
    reset_adapters_for_tests()

    database_engine = create_database_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'gate_escalation.db'}"
    )
    await create_all(database_engine)
    session_factory = create_session_factory(database_engine)
    async with session_factory.begin() as session:
        session.add(CustomerRow(id="cust_a", email="a@example.com", name="Customer A"))
        session.add(
            CaseRow(
                id="case_1",
                customer_id="cust_a",
                opening_message_id="message-1",
                created_at=datetime(2030, 1, 1, tzinfo=UTC),
            )
        )
        await append_canonical_event(
            session,
            event_type=EventType.CASE_OPENED,
            customer_id="cust_a",
            case_id="case_1",
            actor=Actor.SYSTEM,
            channel=Channel.INTERNAL,
            payload={"opening_message_id": "message-1"},
        )
    yield session_factory
    await dispose_database(database_engine)


async def test_escalate_case_records_ticket_id_in_escalated_event(
    gate_escalation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    escalate_action = Escalate(action="escalate", reason="Customer is upset")
    authorisation = Authorisation(
        case_id="case_1",
        action=escalate_action,
        obligations_discharged=applicable_obligations(escalate_action),
    )
    async with gate_escalation_session_factory.begin() as session:
        await escalate_case(
            session,
            "case_1",
            origin=EscalationOrigin.AGENT,
            authorisation=authorisation,
        )

    assert len(ticketing.escalations) == 1
    ticket_id = ticketing.escalations[0].ticket_id

    async with gate_escalation_session_factory() as session:
        case_events = await load_case_events(session, "case_1")
        escalated_event = next(
            event for event in case_events if event.event_type is EventType.ESCALATED
        )
        escalated_payload = EscalatedPayload.model_validate(escalated_event.payload)
        assert escalated_payload.ticket_id == ticket_id


async def test_escalate_case_always_appends_escalated_then_case_closed(
    gate_escalation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    force_escalate = ForceEscalate(
        rule="R_DENIAL_LOOP",
        reason="Denial loop exceeded",
    )
    async with gate_escalation_session_factory.begin() as session:
        await escalate_case(
            session,
            "case_1",
            origin=EscalationOrigin.POLICY,
            force_escalate=force_escalate,
        )

    async with gate_escalation_session_factory() as session:
        case_events = await load_case_events(session, "case_1")
        event_types = [event.event_type for event in case_events]
        assert event_types == [
            EventType.CASE_OPENED,
            EventType.ESCALATED,
            EventType.CASE_CLOSED,
        ]

        case_summary = project_case_summary(
            case_id="case_1",
            customer_id="cust_a",
            events=case_events,
            now=clock.now(),
        )
        assert case_summary.status is CaseStatus.CLOSED


async def test_escalated_actor_is_agent_only_for_agent_origin() -> None:
    assert escalated_actor_for_origin(EscalationOrigin.AGENT) is Actor.AGENT
    assert escalated_actor_for_origin(EscalationOrigin.POLICY) is Actor.SYSTEM
    assert escalated_actor_for_origin(EscalationOrigin.STEP_LIMIT) is Actor.SYSTEM
    assert escalated_actor_for_origin(EscalationOrigin.PARSE_LIMIT) is Actor.SYSTEM
    assert escalated_actor_for_origin(EscalationOrigin.MODEL_FAILURE) is Actor.SYSTEM


async def test_case_closed_outcome_maps_escalation_origin() -> None:
    assert case_outcome_for_escalation_origin(EscalationOrigin.AGENT) is (
        CaseOutcome.ESCALATED
    )
    assert case_outcome_for_escalation_origin(EscalationOrigin.POLICY) is (
        CaseOutcome.ESCALATED
    )
    assert case_outcome_for_escalation_origin(EscalationOrigin.STEP_LIMIT) is (
        CaseOutcome.STEP_LIMIT
    )
    assert case_outcome_for_escalation_origin(EscalationOrigin.PARSE_LIMIT) is (
        CaseOutcome.PARSE_LIMIT
    )
    assert case_outcome_for_escalation_origin(EscalationOrigin.MODEL_FAILURE) is (
        CaseOutcome.MODEL_FAILURE
    )


async def test_escalate_case_writes_both_events_in_one_transaction(
    gate_escalation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with gate_escalation_session_factory.begin() as session:
        await escalate_case(
            session,
            "case_1",
            origin=EscalationOrigin.STEP_LIMIT,
            reason="Step limit reached",
        )

    async with gate_escalation_session_factory() as session:
        case_events = await load_case_events(session, "case_1")
        closed_event = case_events[-1]
        closed_payload = CaseClosedPayload.model_validate(closed_event.payload)
        assert closed_payload.outcome is CaseOutcome.STEP_LIMIT
        assert closed_payload.summary is None
        assert closed_event.actor is Actor.SYSTEM


async def test_agent_escalation_without_authorisation_raises(
    gate_escalation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with gate_escalation_session_factory.begin() as session:
        with pytest.raises(
            AuthorisationError, match="requires a matching authorisation"
        ):
            await escalate_case(
                session,
                "case_1",
                origin=EscalationOrigin.AGENT,
            )

    assert len(ticketing.escalations) == 0


async def test_policy_escalation_without_force_escalate_raises(
    gate_escalation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with gate_escalation_session_factory.begin() as session:
        with pytest.raises(ValueError, match="requires ForceEscalate evidence"):
            await escalate_case(
                session,
                "case_1",
                origin=EscalationOrigin.POLICY,
            )

    assert len(ticketing.escalations) == 0


async def test_agent_escalation_rejects_non_escalate_authorisation(
    gate_escalation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    send_reply = SendReply(action="send_reply", subject="Hi", body="There")
    authorisation = Authorisation(
        case_id="case_1",
        action=send_reply,
        obligations_discharged=applicable_obligations(send_reply),
    )
    async with gate_escalation_session_factory.begin() as session:
        with pytest.raises(AuthorisationError, match="must be Escalate"):
            await escalate_case(
                session,
                "case_1",
                origin=EscalationOrigin.AGENT,
                authorisation=authorisation,
            )

    assert len(ticketing.escalations) == 0


async def test_policy_escalation_rejects_conflicting_reason(
    gate_escalation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    force_escalate = ForceEscalate(rule="R_DENIAL_LOOP", reason="Verdict reason")
    async with gate_escalation_session_factory.begin() as session:
        with pytest.raises(ValueError, match="ForceEscalate evidence"):
            await escalate_case(
                session,
                "case_1",
                origin=EscalationOrigin.POLICY,
                force_escalate=force_escalate,
                reason="Different reason",
            )

    assert len(ticketing.escalations) == 0


async def test_load_rule_context_for_case_folds_open_case(
    gate_escalation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with gate_escalation_session_factory() as session:
        case_row, rule_context = await load_rule_context_for_case(session, "case_1")

    assert case_row.id == "case_1"
    assert rule_context.case.status is CaseStatus.OPEN
    assert rule_context.customer.customer_id == "cust_a"


async def test_escalate_case_increments_customer_event_sequence(
    gate_escalation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with gate_escalation_session_factory.begin() as session:
        await escalate_case(
            session,
            "case_1",
            origin=EscalationOrigin.PARSE_LIMIT,
            reason="Parse limit reached",
        )

    async with gate_escalation_session_factory() as session:
        event_count = await session.scalar(select(func.count()).select_from(EventRow))
        assert event_count == 3

        customer_events = await session.scalars(
            select(EventRow)
            .where(EventRow.customer_id == "cust_a")
            .order_by(EventRow.seq.asc())
        )
        sequences = [event_row.seq for event_row in customer_events]
        assert sequences == [1, 2, 3]

"""Unit tests for verification confirmation through the gate."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund import clock, config
from saferefund.actions.models import RequestVerification
from saferefund.adapters import reset_adapters_for_tests
from saferefund.domain.enums import Actor, CaseStatus, Channel, VerificationMethod
from saferefund.domain.events import EventType
from saferefund.domain.payloads import (
    CustomerVerifiedPayload,
    VerificationRequestedPayload,
)
from saferefund.domain.tables import CaseRow
from saferefund.gate.operations import confirm_verification, execute_agent_action
from saferefund.gate.outcomes import VerificationResultKind
from saferefund.projections.case import project_case_summary
from saferefund.repositories.events import (
    append_canonical_event,
    load_case_events,
    load_customer_events,
)
from saferefund.repositories.seed import TOM_CUSTOMER_ID


async def _open_case(
    session: AsyncSession,
    *,
    case_id: str,
    customer_id: str,
    opening_message_id: str,
    created_at: datetime,
) -> None:
    session.add(
        CaseRow(
            id=case_id,
            customer_id=customer_id,
            opening_message_id=opening_message_id,
            created_at=created_at,
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


@pytest.fixture
def verification_gate_session_factory(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    reset_adapters_for_tests()
    return seeded_session_factory


async def _request_verification_for_case(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    case_id: str,
    customer_id: str,
    opening_message_id: str,
    created_at: datetime,
) -> str:
    async with session_factory.begin() as session:
        await _open_case(
            session,
            case_id=case_id,
            customer_id=customer_id,
            opening_message_id=opening_message_id,
            created_at=created_at,
        )
        await execute_agent_action(
            session,
            case_id,
            RequestVerification(action="request_verification"),
        )

    async with session_factory() as session:
        case_events = await load_case_events(session, case_id)
        verification_event = next(
            event
            for event in case_events
            if event.event_type is EventType.VERIFICATION_REQUESTED
        )
        payload = VerificationRequestedPayload.model_validate(
            verification_event.payload
        )
        return payload.token


async def test_confirm_unknown_token_appends_nothing(
    verification_gate_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with verification_gate_session_factory.begin() as session:
        outcome = await confirm_verification(session, "vrf_unknown_token")

    assert outcome.kind is VerificationResultKind.NOT_FOUND
    assert outcome.customer_id is None
    assert outcome.open_case_ids == ()

    async with verification_gate_session_factory() as session:
        customer_events = await load_customer_events(session, TOM_CUSTOMER_ID)
        verified_events = [
            event
            for event in customer_events
            if event.event_type is EventType.CUSTOMER_VERIFIED
        ]
        assert len(verified_events) == 0


async def test_confirm_expired_token_appends_nothing_and_returns_issuing_case(
    verification_gate_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = await _request_verification_for_case(
        verification_gate_session_factory,
        case_id="case_tom_expired",
        customer_id=TOM_CUSTOMER_ID,
        opening_message_id="msg-tom-expired",
        created_at=datetime(2030, 1, 1, tzinfo=UTC),
    )

    clock.set_now_for_tests(
        datetime(2030, 1, 15, 9, 30, tzinfo=UTC)
        + timedelta(seconds=config.VERIFICATION_TTL_SECONDS + 1)
    )

    async with verification_gate_session_factory.begin() as session:
        outcome = await confirm_verification(session, token)

    assert outcome.kind is VerificationResultKind.EXPIRED
    assert outcome.customer_id == TOM_CUSTOMER_ID
    assert outcome.issuing_case_id == "case_tom_expired"
    assert outcome.open_case_ids == ()

    async with verification_gate_session_factory() as session:
        customer_events = await load_customer_events(session, TOM_CUSTOMER_ID)
        verified_events = [
            event
            for event in customer_events
            if event.event_type is EventType.CUSTOMER_VERIFIED
        ]
        assert len(verified_events) == 0

        customer_events = await load_customer_events(session, TOM_CUSTOMER_ID)
        case_summary = project_case_summary(
            case_id="case_tom_expired",
            customer_id=TOM_CUSTOMER_ID,
            events=customer_events,
            now=clock.now(),
        )
        assert case_summary.status is CaseStatus.OPEN


async def test_confirm_valid_token_appends_customer_verified_and_lists_open_cases(
    verification_gate_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = await _request_verification_for_case(
        verification_gate_session_factory,
        case_id="case_tom_valid",
        customer_id=TOM_CUSTOMER_ID,
        opening_message_id="msg-tom-valid",
        created_at=datetime(2030, 1, 1, tzinfo=UTC),
    )

    async with verification_gate_session_factory.begin() as session:
        outcome = await confirm_verification(session, token)

    assert outcome.kind is VerificationResultKind.VERIFIED
    assert outcome.customer_id == TOM_CUSTOMER_ID
    assert outcome.issuing_case_id is None
    assert outcome.open_case_ids == ("case_tom_valid",)

    async with verification_gate_session_factory() as session:
        customer_events = await load_customer_events(session, TOM_CUSTOMER_ID)
        verified_event = next(
            event
            for event in customer_events
            if event.event_type is EventType.CUSTOMER_VERIFIED
        )
        assert verified_event.actor is Actor.SYSTEM
        assert verified_event.channel is Channel.VERIFICATION_API
        assert verified_event.case_id is None

        verified_payload = CustomerVerifiedPayload.model_validate(
            verified_event.payload
        )
        assert verified_payload.method is VerificationMethod.TOKEN

        customer_events = await load_customer_events(session, TOM_CUSTOMER_ID)
        case_summary = project_case_summary(
            case_id="case_tom_valid",
            customer_id=TOM_CUSTOMER_ID,
            events=customer_events,
            now=clock.now(),
        )
        assert case_summary.status is CaseStatus.OPEN


async def test_confirm_valid_token_returns_all_open_cases_oldest_first(
    verification_gate_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = await _request_verification_for_case(
        verification_gate_session_factory,
        case_id="case_tom_first",
        customer_id=TOM_CUSTOMER_ID,
        opening_message_id="msg-tom-first",
        created_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    await _request_verification_for_case(
        verification_gate_session_factory,
        case_id="case_tom_second",
        customer_id=TOM_CUSTOMER_ID,
        opening_message_id="msg-tom-second",
        created_at=datetime(2030, 1, 2, tzinfo=UTC),
    )

    async with verification_gate_session_factory.begin() as session:
        outcome = await confirm_verification(session, token)

    assert outcome.kind is VerificationResultKind.VERIFIED
    assert outcome.open_case_ids == ("case_tom_first", "case_tom_second")

    async with verification_gate_session_factory() as session:
        customer_events = await load_customer_events(session, TOM_CUSTOMER_ID)
        for case_id in outcome.open_case_ids:
            case_summary = project_case_summary(
                case_id=case_id,
                customer_id=TOM_CUSTOMER_ID,
                events=customer_events,
                now=clock.now(),
            )
            assert case_summary.status is CaseStatus.OPEN

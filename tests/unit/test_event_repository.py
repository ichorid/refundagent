import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund import ids
from saferefund.db import (
    create_all,
    create_database_engine,
    create_session_factory,
    dispose_database,
)
from saferefund.domain.enums import Actor, Channel
from saferefund.domain.events import EventType, InvalidActorChannelError
from saferefund.domain.tables import CaseRow, CustomerRow, EventRow, EventSequenceRow
from saferefund.repositories.events import append_canonical_event, load_customer_events


@pytest.fixture
async def event_repository_session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    ids.reset_counter_for_tests()
    database_engine = create_database_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'event_repository.db'}"
    )
    await create_all(database_engine)
    session_factory = create_session_factory(database_engine)
    async with session_factory.begin() as session:
        session.add_all(
            [
                CustomerRow(id="cust_a", email="a@example.com", name="Customer A"),
                CustomerRow(id="cust_b", email="b@example.com", name="Customer B"),
                CaseRow(
                    id="case_1",
                    customer_id="cust_a",
                    opening_message_id="message-1",
                    created_at=datetime(2030, 1, 1, tzinfo=UTC),
                ),
                CaseRow(
                    id="case_2",
                    customer_id="cust_a",
                    opening_message_id="message-2",
                    created_at=datetime(2030, 1, 2, tzinfo=UTC),
                ),
                CaseRow(
                    id="case_b",
                    customer_id="cust_b",
                    opening_message_id="message-b",
                    created_at=datetime(2030, 1, 1, tzinfo=UTC),
                ),
            ]
        )
    yield session_factory
    await dispose_database(database_engine)


async def _append_case_opened(
    session: AsyncSession,
    *,
    customer_id: str,
    case_id: str,
    opening_message_id: str,
) -> None:
    await append_canonical_event(
        session,
        event_type=EventType.CASE_OPENED,
        customer_id=customer_id,
        case_id=case_id,
        actor=Actor.SYSTEM,
        channel=Channel.INTERNAL,
        payload={"opening_message_id": opening_message_id},
    )


async def test_sequential_appends_assign_monotonic_customer_sequence(
    event_repository_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with event_repository_session_factory.begin() as session:
        await _append_case_opened(
            session,
            customer_id="cust_a",
            case_id="case_1",
            opening_message_id="message-1",
        )
        await _append_case_opened(
            session,
            customer_id="cust_a",
            case_id="case_2",
            opening_message_id="message-2",
        )
        await append_canonical_event(
            session,
            event_type=EventType.EMAIL_RECEIVED,
            customer_id="cust_a",
            case_id="case_2",
            actor=Actor.CUSTOMER,
            channel=Channel.EMAIL,
            payload={
                "message_id": "message-2",
                "subject": "Refund",
                "body": "Please refund my order.",
            },
        )

    async with event_repository_session_factory() as session:
        customer_events = await load_customer_events(session, "cust_a")

    assert [event.seq for event in customer_events] == [1, 2, 3]


async def test_each_customer_starts_event_sequence_at_one(
    event_repository_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with event_repository_session_factory.begin() as session:
        await _append_case_opened(
            session,
            customer_id="cust_a",
            case_id="case_1",
            opening_message_id="message-a",
        )
        await _append_case_opened(
            session,
            customer_id="cust_b",
            case_id="case_b",
            opening_message_id="message-b",
        )

    async with event_repository_session_factory() as session:
        customer_a_events = await load_customer_events(session, "cust_a")
        customer_b_events = await load_customer_events(session, "cust_b")

    assert customer_a_events[0].seq == 1
    assert customer_b_events[0].seq == 1


async def test_invalid_actor_channel_pair_is_not_persisted(
    event_repository_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with event_repository_session_factory.begin() as session:
        with pytest.raises(InvalidActorChannelError):
            await append_canonical_event(
                session,
                event_type=EventType.CASE_OPENED,
                customer_id="cust_a",
                case_id="case_1",
                actor=Actor.AGENT,
                channel=Channel.INTERNAL,
                payload={"opening_message_id": "message-1"},
            )

    async with event_repository_session_factory() as session:
        persisted_event_count = await session.scalar(
            select(func.count()).select_from(EventRow)
        )

    assert persisted_event_count == 0


async def test_event_sequence_counter_tracks_last_allocated_sequence(
    event_repository_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with event_repository_session_factory.begin() as session:
        await _append_case_opened(
            session,
            customer_id="cust_a",
            case_id="case_1",
            opening_message_id="message-1",
        )
        await _append_case_opened(
            session,
            customer_id="cust_a",
            case_id="case_2",
            opening_message_id="message-2",
        )

    async with event_repository_session_factory() as session:
        counter_row = await session.get(EventSequenceRow, "cust_a")

    assert counter_row is not None
    assert counter_row.next_seq == 2


async def test_rolled_back_sequence_allocation_is_reused_on_retry(
    event_repository_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with event_repository_session_factory.begin() as session:
        await _append_case_opened(
            session,
            customer_id="cust_a",
            case_id="case_1",
            opening_message_id="message-1",
        )

    async with event_repository_session_factory() as session:
        await append_canonical_event(
            session,
            event_type=EventType.EMAIL_RECEIVED,
            customer_id="cust_a",
            case_id="case_1",
            actor=Actor.CUSTOMER,
            channel=Channel.EMAIL,
            payload={
                "message_id": "rollback-probe",
                "subject": "Rollback",
                "body": "Probe",
            },
        )
        await session.rollback()

    async with event_repository_session_factory.begin() as session:
        stored_event = await append_canonical_event(
            session,
            event_type=EventType.EMAIL_RECEIVED,
            customer_id="cust_a",
            case_id="case_1",
            actor=Actor.CUSTOMER,
            channel=Channel.EMAIL,
            payload={
                "message_id": "rollback-retry",
                "subject": "Retry",
                "body": "After rollback",
            },
        )

    async with event_repository_session_factory() as session:
        customer_events = await load_customer_events(session, "cust_a")
        counter_row = await session.get(EventSequenceRow, "cust_a")

    assert stored_event.seq == 2
    assert [event.seq for event in customer_events] == [1, 2]
    assert counter_row is not None
    assert counter_row.next_seq == 2


async def test_concurrent_appends_from_separate_sessions_assign_contiguous_sequences(
    event_repository_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """SQLite serialises writers; proves atomic allocation only, not PG contention."""
    async with event_repository_session_factory.begin() as session:
        await _append_case_opened(
            session,
            customer_id="cust_a",
            case_id="case_1",
            opening_message_id="message-1",
        )

    append_count = 8
    barrier = asyncio.Barrier(append_count)

    async def append_from_isolated_session(index: int) -> None:
        async with (
            event_repository_session_factory() as session,
            session.begin(),
        ):
            await barrier.wait()
            await append_canonical_event(
                session,
                event_type=EventType.EMAIL_RECEIVED,
                customer_id="cust_a",
                case_id="case_1",
                actor=Actor.CUSTOMER,
                channel=Channel.EMAIL,
                payload={
                    "message_id": f"concurrent-{index}",
                    "subject": f"Concurrent {index}",
                    "body": f"Body {index}",
                },
            )

    await asyncio.gather(
        *(append_from_isolated_session(index) for index in range(append_count))
    )

    async with event_repository_session_factory() as session:
        customer_events = await load_customer_events(session, "cust_a")
        counter_row = await session.get(EventSequenceRow, "cust_a")

    allocated_sequences = [event.seq for event in customer_events if event.seq != 1]
    assert allocated_sequences == list(range(2, append_count + 2))
    assert len(customer_events) == append_count + 1
    assert len({event.seq for event in customer_events}) == append_count + 1
    assert counter_row is not None
    assert counter_row.next_seq == append_count + 1

"""PostgreSQL concurrency evidence for atomic event sequence allocation.

Evidence applies only to PostgreSQL {version} (see tests/postgres/conftest.py).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from saferefund.domain.enums import Actor, Channel
from saferefund.domain.events import EventType
from saferefund.domain.tables import CaseRow, CustomerRow, EventRow, EventSequenceRow
from saferefund.repositories.customers import normalized_email
from saferefund.repositories.events import append_canonical_event, load_customer_events
from tests.postgres.support.coordination import (
    concurrent_start_barrier,
    run_after_in_transaction_barrier,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CUSTOMER_ID = "cust_pg_seq"
CASE_ID = "case_pg_seq"
FIRST_WAVE_PARTICIPANTS = 4
SUBSEQUENT_WAVE_PARTICIPANTS = 6


@pytest.mark.postgres
async def test_concurrent_first_and_subsequent_appends_allocate_unique_monotonic_sequences(
    postgres_seeded_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Concurrent first and later appends stay unique and monotonic on PostgreSQL."""
    async with postgres_seeded_session_factory.begin() as session:
        session.add(
            CustomerRow(
                id=CUSTOMER_ID,
                email=normalized_email("pg-seq@example.com"),
                name="PostgreSQL Sequence Probe",
            )
        )
        session.add(
            CaseRow(
                id=CASE_ID,
                customer_id=CUSTOMER_ID,
                opening_message_id="msg-pg-seq-open",
                created_at=datetime(2030, 1, 1, tzinfo=UTC),
            )
        )

    async with postgres_seeded_session_factory() as session:
        sequence_row_before_first_wave = await session.get(
            EventSequenceRow,
            CUSTOMER_ID,
        )

    assert sequence_row_before_first_wave is None

    first_barrier = concurrent_start_barrier(FIRST_WAVE_PARTICIPANTS)
    subsequent_barrier = concurrent_start_barrier(SUBSEQUENT_WAVE_PARTICIPANTS)

    async def append_first_wave(participant_id: int) -> int:
        backend_pid, _ = await run_after_in_transaction_barrier(
            postgres_seeded_session_factory,
            first_barrier,
            lambda session: append_canonical_event(
                session,
                event_type=EventType.EMAIL_RECEIVED,
                customer_id=CUSTOMER_ID,
                case_id=CASE_ID,
                actor=Actor.CUSTOMER,
                channel=Channel.EMAIL,
                payload={
                    "message_id": f"first-wave-{participant_id}",
                    "subject": "First wave",
                    "body": f"Participant {participant_id}",
                },
            ),
        )
        return backend_pid

    first_wave_backend_pids = await asyncio.gather(
        *(append_first_wave(index) for index in range(FIRST_WAVE_PARTICIPANTS))
    )

    assert len(set(first_wave_backend_pids)) == FIRST_WAVE_PARTICIPANTS

    async with postgres_seeded_session_factory() as session:
        counter_after_first_wave = await session.get(EventSequenceRow, CUSTOMER_ID)
        first_wave_events = await load_customer_events(session, CUSTOMER_ID)

    assert counter_after_first_wave is not None
    assert counter_after_first_wave.next_seq == FIRST_WAVE_PARTICIPANTS
    first_wave_sequences = [event.seq for event in first_wave_events]
    assert first_wave_sequences == list(range(1, FIRST_WAVE_PARTICIPANTS + 1))
    assert len(set(first_wave_sequences)) == FIRST_WAVE_PARTICIPANTS

    async def append_subsequent_wave(participant_id: int) -> int:
        backend_pid, _ = await run_after_in_transaction_barrier(
            postgres_seeded_session_factory,
            subsequent_barrier,
            lambda session: append_canonical_event(
                session,
                event_type=EventType.EMAIL_RECEIVED,
                customer_id=CUSTOMER_ID,
                case_id=CASE_ID,
                actor=Actor.CUSTOMER,
                channel=Channel.EMAIL,
                payload={
                    "message_id": f"subsequent-wave-{participant_id}",
                    "subject": "Subsequent wave",
                    "body": f"Participant {participant_id}",
                },
            ),
        )
        return backend_pid

    subsequent_wave_backend_pids = await asyncio.gather(
        *(
            append_subsequent_wave(index)
            for index in range(SUBSEQUENT_WAVE_PARTICIPANTS)
        )
    )

    assert len(set(subsequent_wave_backend_pids)) == SUBSEQUENT_WAVE_PARTICIPANTS

    total_events = FIRST_WAVE_PARTICIPANTS + SUBSEQUENT_WAVE_PARTICIPANTS

    async with postgres_seeded_session_factory() as session:
        customer_events = await load_customer_events(session, CUSTOMER_ID)
        counter_row = await session.get(EventSequenceRow, CUSTOMER_ID)
        persisted_sequences = (
            await session.scalars(
                select(EventRow.seq)
                .where(EventRow.customer_id == CUSTOMER_ID)
                .order_by(EventRow.seq.asc())
            )
        ).all()

    assert counter_row is not None
    assert counter_row.next_seq == total_events
    assert persisted_sequences == list(range(1, total_events + 1))
    assert [event.seq for event in customer_events] == list(range(1, total_events + 1))
    assert len({event.seq for event in customer_events}) == total_events

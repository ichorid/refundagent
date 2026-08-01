"""Unit tests for unknown-sender gate reply without persistence."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund import config, ids
from saferefund.adapters import mailer, reset_adapters_for_tests
from saferefund.db import (
    create_all,
    create_database_engine,
    create_session_factory,
    dispose_database,
)
from saferefund.domain.tables import EventRow
from saferefund.gate.operations import send_unknown_sender_reply
from saferefund.repositories.seed import UNKNOWN_SENDER_EMAIL, seed_database


@pytest.fixture
async def unknown_sender_session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    ids.reset_counter_for_tests()
    reset_adapters_for_tests()

    database_engine = create_database_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'unknown_sender.db'}"
    )
    await create_all(database_engine)
    session_factory = create_session_factory(database_engine)
    async with session_factory.begin() as session:
        await seed_database(session)
    yield session_factory
    await dispose_database(database_engine)


async def test_send_unknown_sender_reply_queues_canned_mail(
    unknown_sender_session_factory: async_sessionmaker[AsyncSession],  # noqa: ARG001
) -> None:
    await send_unknown_sender_reply(UNKNOWN_SENDER_EMAIL)

    assert len(mailer.outbox) == 1
    message = mailer.outbox[0]
    assert message.to == UNKNOWN_SENDER_EMAIL
    assert message.subject == config.UNKNOWN_SENDER_SUBJECT
    assert message.body == config.UNKNOWN_SENDER_BODY


async def test_send_unknown_sender_reply_writes_zero_events(
    unknown_sender_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with unknown_sender_session_factory() as session:
        event_count_before = await session.scalar(
            select(func.count()).select_from(EventRow)
        )

    await send_unknown_sender_reply(UNKNOWN_SENDER_EMAIL)

    async with unknown_sender_session_factory() as session:
        event_count_after = await session.scalar(
            select(func.count()).select_from(EventRow)
        )
        assert event_count_after == event_count_before
        assert event_count_before == 1

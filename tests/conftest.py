"""Shared pytest fixtures for database reset, seeding, and deterministic primitives."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from saferefund import clock, ids
from saferefund.db import (
    create_all,
    create_database_engine,
    create_session_factory,
    dispose_database,
    reset_database,
)
from saferefund.repositories.seed import SeedProfile, seed_database

FIXED_TEST_NOW = datetime(2030, 1, 15, 9, 30, tzinfo=UTC)


def reset_deterministic_primitives() -> None:
    """Reset ID generation and the application clock before each seeded scenario."""
    ids.reset_counter_for_tests()
    clock.reset_now_for_tests()
    clock.set_now_for_tests(FIXED_TEST_NOW)


@pytest.fixture
async def database_engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    """Provide one isolated async SQLite engine per test."""
    database_engine = create_database_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    )
    await create_all(database_engine)
    yield database_engine
    await dispose_database(database_engine)


@pytest.fixture
def session_factory(
    database_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Provide a session factory bound to the isolated test database."""
    return create_session_factory(database_engine)


@pytest.fixture
async def seeded_session_factory(
    session_factory: async_sessionmaker[AsyncSession],
    database_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Reset the database and load the standard seed profile."""
    reset_deterministic_primitives()
    await reset_database(database_engine)
    async with session_factory.begin() as session:
        await seed_database(session, profile=SeedProfile.STANDARD)
    return session_factory


@pytest.fixture
async def injected_seed_session_factory(
    session_factory: async_sessionmaker[AsyncSession],
    database_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Reset the database and load the injection-variant seed profile."""
    reset_deterministic_primitives()
    await reset_database(database_engine)
    async with session_factory.begin() as session:
        await seed_database(session, profile=SeedProfile.INJECTED_ORD_1001_ITEM)
    return session_factory

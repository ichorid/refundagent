"""Fixtures for the architecture-invariant proof tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from saferefund.adapters import reset_adapters_for_tests
from saferefund.agent.locks import reset_case_locks_for_tests

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture
def api_session_factory(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """Provide a seeded database with adapter and lock state reset for HTTP tests."""
    reset_adapters_for_tests()
    reset_case_locks_for_tests()
    return seeded_session_factory


@pytest.fixture(autouse=True)
def _reset_process_state() -> None:
    """Clear in-process adapter and single-flight state before every proof test."""
    reset_adapters_for_tests()
    reset_case_locks_for_tests()

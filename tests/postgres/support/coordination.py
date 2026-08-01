"""Start barriers for PostgreSQL concurrency schedules."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def concurrent_start_barrier(participants: int) -> asyncio.Barrier:
    """Return a barrier that releases every participant into the race together."""
    return asyncio.Barrier(participants)


async def read_backend_pid(session: AsyncSession) -> int:
    """Return the PostgreSQL backend PID for the connection backing one session."""
    backend_pid = await session.scalar(text("SELECT pg_backend_pid()"))
    if backend_pid is None:
        message = "pg_backend_pid() returned NULL"
        raise RuntimeError(message)
    return int(backend_pid)


async def run_after_in_transaction_barrier[T](
    session_factory: async_sessionmaker[AsyncSession],
    barrier: asyncio.Barrier,
    operation: Callable[[AsyncSession], Awaitable[T]],
) -> tuple[int, T]:
    """Begin one transaction, synchronize at the barrier, then run the operation."""
    async with session_factory.begin() as session:
        backend_pid = await read_backend_pid(session)
        await barrier.wait()
        return backend_pid, await operation(session)

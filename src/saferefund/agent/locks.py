"""In-process single-flight locks serialising agent loop execution per case."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_case_locks: dict[str, asyncio.Lock] = {}
_registry_lock = asyncio.Lock()


@asynccontextmanager
async def case_execution_lock(case_id: str) -> AsyncIterator[None]:
    """Hold one asyncio lock for an entire loop run on the given case."""
    async with _registry_lock:
        case_lock = _case_locks.setdefault(case_id, asyncio.Lock())
    async with case_lock:
        yield


def reset_case_locks_for_tests() -> None:
    """Clear the lock registry so unit tests start from an empty map."""
    _case_locks.clear()


__all__ = ["case_execution_lock", "reset_case_locks_for_tests"]

"""Unit coverage for customer advisory-lock orchestration helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from saferefund.repositories.customers import hold_customer_advisory_lock

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _assert_acquire_release_use_same_pinned_session_and_customer(
    acquire: AsyncMock,
    release: AsyncMock,
    *,
    customer_id: str,
) -> None:
    acquire.assert_awaited_once()
    release.assert_awaited_once()
    assert acquire.await_args is not None
    assert release.await_args is not None
    acquire_session, acquire_customer = acquire.await_args.args
    release_session, release_customer = release.await_args.args
    assert acquire_session is release_session, (
        "release must use the same pinned session as acquire"
    )
    assert acquire_customer == customer_id
    assert release_customer == customer_id


@pytest.mark.asyncio
async def test_hold_customer_advisory_lock_releases_after_success(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    acquire = AsyncMock()
    release = AsyncMock()
    customer_id = "cust_test"
    with (
        patch(
            "saferefund.repositories.customers.acquire_customer_advisory_lock",
            acquire,
        ),
        patch(
            "saferefund.repositories.customers.release_customer_advisory_lock",
            release,
        ),
    ):
        async with hold_customer_advisory_lock(session_factory, customer_id) as session:
            assert acquire.await_args is not None
            assert session is acquire.await_args.args[0]

    _assert_acquire_release_use_same_pinned_session_and_customer(
        acquire,
        release,
        customer_id=customer_id,
    )


@pytest.mark.asyncio
async def test_hold_customer_advisory_lock_releases_after_exception(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    acquire = AsyncMock()
    release = AsyncMock()
    customer_id = "cust_test"

    async def _raise_after_acquire() -> None:
        async with hold_customer_advisory_lock(session_factory, customer_id):
            message = "boom"
            raise RuntimeError(message)

    with (
        patch(
            "saferefund.repositories.customers.acquire_customer_advisory_lock",
            acquire,
        ),
        patch(
            "saferefund.repositories.customers.release_customer_advisory_lock",
            release,
        ),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await _raise_after_acquire()

    _assert_acquire_release_use_same_pinned_session_and_customer(
        acquire,
        release,
        customer_id=customer_id,
    )

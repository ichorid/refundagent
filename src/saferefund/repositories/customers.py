"""Customer seed-row lookups and row locking for event writes."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund.domain.tables import CustomerRow

_CUSTOMER_ADVISORY_LOCK_NAMESPACE = 0x53414652


class CustomerNotFoundError(LookupError):
    """Raised when a customer identifier does not match a seed row."""

    def __init__(self, customer_id: str) -> None:
        """Record the missing customer identifier."""
        super().__init__(f"Customer not found: {customer_id}")


def normalized_email(email: str) -> str:
    """Lowercase an email address for storage and lookup."""
    return email.lower()


async def find_customer_by_email(
    session: AsyncSession,
    email: str,
) -> CustomerRow | None:
    """Return the customer seed row for a lowercased email address, if present."""
    statement = select(CustomerRow).where(CustomerRow.email == normalized_email(email))
    customer_row = await session.scalar(statement)
    if customer_row is None:
        return None
    return customer_row


async def lock_customer_for_update(
    session: AsyncSession,
    customer_id: str,
) -> CustomerRow:
    """Lock the customer row before event history reads or sequence assignment."""
    statement = (
        select(CustomerRow).where(CustomerRow.id == customer_id).with_for_update()
    )
    customer_row = await session.scalar(statement)
    if customer_row is None:
        raise CustomerNotFoundError(customer_id)
    return customer_row


def _session_uses_postgresql(session: AsyncSession) -> bool:
    bind = session.get_bind()
    return bind.dialect.name == "postgresql"


async def acquire_customer_advisory_lock(
    session: AsyncSession,
    customer_id: str,
) -> None:
    """Block cross-connection races until refund payment and execution finish."""
    if not _session_uses_postgresql(session):
        return
    await session.execute(
        text("SELECT pg_advisory_lock(:namespace, hashtext(:customer_id))"),
        {
            "namespace": _CUSTOMER_ADVISORY_LOCK_NAMESPACE,
            "customer_id": customer_id,
        },
    )


async def release_customer_advisory_lock(
    session: AsyncSession,
    customer_id: str,
) -> None:
    """Release the PostgreSQL advisory lock acquired for one customer."""
    if not _session_uses_postgresql(session):
        return
    unlocked = await session.scalar(
        text("SELECT pg_advisory_unlock(:namespace, hashtext(:customer_id))"),
        {
            "namespace": _CUSTOMER_ADVISORY_LOCK_NAMESPACE,
            "customer_id": customer_id,
        },
    )
    if not unlocked:
        message = f"failed to release advisory lock for customer {customer_id}"
        raise RuntimeError(message)


@asynccontextmanager
async def hold_customer_advisory_lock(
    session_factory: async_sessionmaker[AsyncSession],
    customer_id: str,
) -> AsyncIterator[AsyncSession]:
    """Acquire and release one customer advisory lock on a pinned connection."""
    async with session_factory() as advisory_session:
        await acquire_customer_advisory_lock(advisory_session, customer_id)
        try:
            yield advisory_session
        finally:
            await release_customer_advisory_lock(advisory_session, customer_id)

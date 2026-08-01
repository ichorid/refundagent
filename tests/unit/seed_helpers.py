"""Shared helpers for seed data unit and integration tests."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from saferefund.domain.tables import CustomerRow, EventRow, OrderRow


async def count_seed_rows(session: AsyncSession) -> tuple[int, int, int]:
    """Return customer, order, and event row counts for seed assertions."""
    customer_count = await session.scalar(select(func.count()).select_from(CustomerRow))
    order_count = await session.scalar(select(func.count()).select_from(OrderRow))
    event_count = await session.scalar(select(func.count()).select_from(EventRow))
    return customer_count or 0, order_count or 0, event_count or 0

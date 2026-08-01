"""Immutable order seed-row queries."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saferefund.domain.tables import OrderRow


async def list_orders_for_customer(
    session: AsyncSession,
    customer_id: str,
) -> list[OrderRow]:
    """Return every order seed row owned by the customer."""
    statement = (
        select(OrderRow)
        .where(OrderRow.customer_id == customer_id)
        .order_by(OrderRow.id)
    )
    result = await session.scalars(statement)
    return list(result.all())


async def find_order_by_id(
    session: AsyncSession,
    order_id: str,
) -> OrderRow | None:
    """Return one order seed row when it exists."""
    return await session.get(OrderRow, order_id)


async def load_disclosed_order_rows(
    session: AsyncSession,
    customer_id: str,
    order_ids: frozenset[str],
) -> list[OrderRow]:
    """Load only disclosed order rows that belong to the customer."""
    if not order_ids:
        return []
    disclosed_rows: list[OrderRow] = []
    for order_id in sorted(order_ids):
        order_row = await find_order_by_id(session, order_id)
        if order_row is not None and order_row.customer_id == customer_id:
            disclosed_rows.append(order_row)
    return disclosed_rows

"""Relational scope validation for customer, case, and order identifiers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from saferefund.domain.events import InvalidEventScopeError
from saferefund.domain.tables import CaseRow, OrderRow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class InvalidEventRelationalScopeError(InvalidEventScopeError):
    """Raised when case_id or order_id do not belong to the stated customer."""


async def _load_case_customer_id(
    session: AsyncSession,
    case_id: str,
) -> str | None:
    customer_id = await session.scalar(
        select(CaseRow.customer_id).where(CaseRow.id == case_id),
    )
    return str(customer_id) if customer_id is not None else None


async def _load_order_customer_id(
    session: AsyncSession,
    order_id: str,
) -> str | None:
    customer_id = await session.scalar(
        select(OrderRow.customer_id).where(OrderRow.id == order_id),
    )
    return str(customer_id) if customer_id is not None else None


async def validate_event_relational_scope(
    session: AsyncSession,
    *,
    customer_id: str,
    case_id: str | None,
    order_id: str | None,
) -> None:
    """Reject case or order identifiers that do not belong to ``customer_id``."""
    case_customer_id: str | None = None
    if case_id is not None:
        case_customer_id = await _load_case_customer_id(session, case_id)
        if case_customer_id is None:
            message = f"case {case_id} does not exist"
            raise InvalidEventRelationalScopeError(message)
        if case_customer_id != customer_id:
            message = (
                f"case {case_id} belongs to customer {case_customer_id}, "
                f"not {customer_id}"
            )
            raise InvalidEventRelationalScopeError(message)

    if order_id is not None:
        order_customer_id = await _load_order_customer_id(session, order_id)
        if order_customer_id is None:
            message = f"order {order_id} does not exist"
            raise InvalidEventRelationalScopeError(message)
        if order_customer_id != customer_id:
            message = (
                f"order {order_id} belongs to customer {order_customer_id}, "
                f"not {customer_id}"
            )
            raise InvalidEventRelationalScopeError(message)

    if case_id is not None and order_id is not None and case_customer_id is not None:
        order_customer_id = await _load_order_customer_id(session, order_id)
        if order_customer_id != case_customer_id:
            message = (
                f"case {case_id} and order {order_id} belong to different customers"
            )
            raise InvalidEventRelationalScopeError(message)


async def validate_refund_relational_scope(
    session: AsyncSession,
    *,
    case_id: str,
    order_id: str,
) -> str:
    """Return the shared customer id or raise when case and order disagree."""
    case_customer_id = await _load_case_customer_id(session, case_id)
    if case_customer_id is None:
        message = f"case {case_id} does not exist"
        raise InvalidEventRelationalScopeError(message)

    order_customer_id = await _load_order_customer_id(session, order_id)
    if order_customer_id is None:
        message = f"order {order_id} does not exist"
        raise InvalidEventRelationalScopeError(message)

    if case_customer_id != order_customer_id:
        message = (
            f"case {case_id} belongs to customer {case_customer_id} but "
            f"order {order_id} belongs to customer {order_customer_id}"
        )
        raise InvalidEventRelationalScopeError(message)

    return case_customer_id

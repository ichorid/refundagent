"""Refund enforcement-row queries."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from saferefund.domain.enums import RefundStatus
from saferefund.domain.tables import RefundRow


def approval_window_is_open(
    approval_expires_at: datetime | None, now: datetime
) -> bool:
    """Return whether the approval window is open at `now`, exclusive of its end."""
    if approval_expires_at is None:
        return False
    return now < approval_expires_at


def active_pending_refund_clause(now: datetime) -> ColumnElement[bool]:
    """Build SQL for refunds still awaiting approval with an open window."""
    return RefundRow.approval_expires_at > now


def due_for_expiry_clause(now: datetime) -> ColumnElement[bool]:
    """Build SQL for pending refunds whose approval window has closed."""
    return RefundRow.approval_expires_at <= now


class RefundNotFoundError(LookupError):
    """Raised when a refund identifier does not match an enforcement row."""

    def __init__(self, refund_id: str) -> None:
        """Record the missing refund identifier."""
        super().__init__(f"Refund not found: {refund_id}")


async def find_refund_by_id(
    session: AsyncSession,
    refund_id: str,
) -> RefundRow | None:
    """Return one refund enforcement row when it exists."""
    return await session.get(RefundRow, refund_id)


async def require_refund_by_id(
    session: AsyncSession,
    refund_id: str,
) -> RefundRow:
    """Return a refund row or raise RefundNotFoundError."""
    refund_row = await find_refund_by_id(session, refund_id)
    if refund_row is None:
        raise RefundNotFoundError(refund_id)
    return refund_row


async def find_open_refund_for_order(
    session: AsyncSession,
    order_id: str,
) -> RefundRow | None:
    """Return the live pending or approved refund row for an order, if any."""
    statement = select(RefundRow).where(
        RefundRow.order_id == order_id,
        RefundRow.status.in_((RefundStatus.PENDING_APPROVAL, RefundStatus.APPROVED)),
    )
    refund_row = await session.scalar(statement)
    if refund_row is None:
        return None
    return refund_row


async def list_active_pending_refunds(
    session: AsyncSession,
    now: datetime,
) -> list[RefundRow]:
    """Return pending refunds whose approval window has not elapsed."""
    statement = (
        select(RefundRow)
        .where(
            RefundRow.status == RefundStatus.PENDING_APPROVAL,
            active_pending_refund_clause(now),
        )
        .order_by(RefundRow.approval_expires_at, RefundRow.id)
    )
    return list(await session.scalars(statement))

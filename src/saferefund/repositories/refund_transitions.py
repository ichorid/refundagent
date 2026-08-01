"""Guarded refund status transitions and the allowed lifecycle matrix."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import update

from saferefund.domain.enums import RefundStatus
from saferefund.domain.tables import RefundRow, RefundStatusTransitionError

if TYPE_CHECKING:
    from decimal import Decimal

    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession


_ALLOWED_REFUND_STATUS_TRANSITIONS: dict[RefundStatus, frozenset[RefundStatus]] = {
    RefundStatus.PENDING_APPROVAL: frozenset(
        {
            RefundStatus.APPROVED,
            RefundStatus.REJECTED,
            RefundStatus.EXPIRED,
        }
    ),
    RefundStatus.APPROVED: frozenset({RefundStatus.EXECUTED}),
}


def allowed_refund_status_targets(from_status: RefundStatus) -> frozenset[RefundStatus]:
    """Return the statuses reachable from ``from_status`` via guarded SQL."""
    return _ALLOWED_REFUND_STATUS_TRANSITIONS.get(from_status, frozenset())


def assert_refund_status_transition_permitted(
    from_status: RefundStatus,
    to_status: RefundStatus,
) -> None:
    """Reject transitions outside the documented refund lifecycle matrix."""
    permitted_targets = allowed_refund_status_targets(from_status)
    if to_status not in permitted_targets:
        message = (
            f"refund status transition {from_status.value} -> {to_status.value} "
            "is not permitted"
        )
        raise RefundStatusTransitionError(message)


@dataclass(frozen=True, slots=True)
class RefundIntentBinding:
    """Immutable refund-authorization fields bound into guarded status updates."""

    order_id: str
    case_id: str
    amount: Decimal


async def transition_refund_status(  # noqa: PLR0913
    session: AsyncSession,
    refund_id: str,
    *,
    from_status: RefundStatus,
    to_status: RefundStatus,
    intent_binding: RefundIntentBinding | None = None,
    clear_approval_expires_at: bool = True,
) -> bool:
    """Atomically move one refund row along an allowed lifecycle edge.

    Returns True when exactly one row was updated. A zero rowcount means another
    transaction already acted on this refund or the immutable binding did not match.
    """
    assert_refund_status_transition_permitted(from_status, to_status)

    values: dict[str, Any] = {"status": to_status}
    if clear_approval_expires_at:
        values["approval_expires_at"] = None

    statement = (
        update(RefundRow)
        .where(
            RefundRow.id == refund_id,
            RefundRow.status == from_status,
        )
        .values(**values)
    )
    if intent_binding is not None:
        statement = statement.where(
            RefundRow.order_id == intent_binding.order_id,
            RefundRow.case_id == intent_binding.case_id,
            RefundRow.amount == intent_binding.amount,
        )

    cursor_result = cast(
        "CursorResult[Any]",
        await session.execute(statement),
    )
    return cursor_result.rowcount == 1


__all__ = [
    "RefundIntentBinding",
    "RefundStatusTransitionError",
    "allowed_refund_status_targets",
    "assert_refund_status_transition_permitted",
    "transition_refund_status",
]

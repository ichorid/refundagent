"""The approval window's two comparisons are exact complements.

Expiry uses ``approval_expires_at <= now``; the active pending queue uses
``approval_expires_at > now``. At exactly ``approval_expires_at`` the refund is
expired, absent from the operator queue, and never approvable or payable.
"""

from __future__ import annotations

from datetime import UTC, timedelta
from typing import TYPE_CHECKING

from saferefund import clock, config
from saferefund.adapters import payment
from saferefund.domain.enums import RefundStatus
from saferefund.gate.common import expire_due_refunds_for_customer
from saferefund.gate.operations import approve_refund
from saferefund.gate.outcomes import OperatorResultKind
from saferefund.repositories.refunds import (
    find_refund_by_id,
    list_active_pending_refunds,
)
from saferefund.repositories.seed import SOPHIE_CUSTOMER_ID
from tests.conftest import FIXED_TEST_NOW
from tests.invariants.scenario import propose_refund_awaiting_approval

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CASE_ID = "case_expiry_boundary"
EXPIRY_INSTANT = FIXED_TEST_NOW + timedelta(seconds=config.APPROVAL_TTL_SECONDS)


async def _pending_refund_at_expiry_instant(
    session_factory: async_sessionmaker[AsyncSession],
) -> str:
    refund_id = await propose_refund_awaiting_approval(
        session_factory,
        case_id=CASE_ID,
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-expiry-boundary",
    )
    async with session_factory() as session:
        refund_row = await find_refund_by_id(session, refund_id)
    assert refund_row is not None
    stored_expiry = refund_row.approval_expires_at
    assert stored_expiry is not None
    # SQLite returns naive datetimes for DateTime(timezone=True) columns.
    assert stored_expiry.replace(tzinfo=UTC) == EXPIRY_INSTANT
    clock.set_now_for_tests(EXPIRY_INSTANT)
    return refund_id


async def test_refund_is_expired_at_exactly_its_expiry_instant(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mutation: use ``<`` instead of ``<=`` in ``due_for_expiry_clause``."""
    refund_id = await _pending_refund_at_expiry_instant(seeded_session_factory)

    async with seeded_session_factory.begin() as session:
        await expire_due_refunds_for_customer(
            session,
            customer_id=SOPHIE_CUSTOMER_ID,
        )

    async with seeded_session_factory() as session:
        refund_row = await find_refund_by_id(session, refund_id)

    assert refund_row is not None
    assert refund_row.status is RefundStatus.EXPIRED


async def test_refund_invisible_to_the_pending_queue_is_never_approvable(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mutation: let approve_refund pay a refund absent from the active queue."""
    refund_id = await _pending_refund_at_expiry_instant(seeded_session_factory)
    payment.calls.clear()

    async with seeded_session_factory() as session:
        active_pending_refunds = await list_active_pending_refunds(
            session,
            EXPIRY_INSTANT,
        )
    assert refund_id not in {refund.id for refund in active_pending_refunds}

    async with seeded_session_factory.begin() as session:
        outcome = await approve_refund(
            session,
            refund_id,
            "operator-at-boundary",
            session_factory=seeded_session_factory,
        )

    assert outcome.kind is OperatorResultKind.CONFLICT
    assert outcome.refund_status is RefundStatus.EXPIRED
    assert payment.calls == []


def test_approval_window_predicate_is_exclusive_at_its_end() -> None:
    """Mutation: change ``now < approval_expires_at`` to an inclusive comparison."""
    from saferefund.repositories import refunds as refunds_repository

    is_window_open = getattr(refunds_repository, "approval_window_is_open", None)
    assert is_window_open is not None, (
        "the approval window boundary must be a single named predicate reused by "
        "the expiry sweep and the operator queue"
    )
    assert is_window_open(EXPIRY_INSTANT, EXPIRY_INSTANT) is False
    assert is_window_open(EXPIRY_INSTANT + timedelta(seconds=1), EXPIRY_INSTANT) is True

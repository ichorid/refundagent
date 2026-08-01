"""SQLite proves ``approve_refund`` reads decisive status only after the customer lock.

Deterministic lock-order injection schedules a competing approval because SQLite
cannot reproduce PostgreSQL row-lock contention between threads. PostgreSQL
contention evidence lives in ``tests/postgres/test_operator_concurrency.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from saferefund.adapters import payment
from saferefund.domain.enums import RefundStatus
from saferefund.domain.events import EventType
from saferefund.gate import operator as operator_gate
from saferefund.gate.operations import approve_refund
from saferefund.gate.outcomes import OperatorResultKind
from saferefund.repositories.events import load_case_events
from saferefund.repositories.refunds import find_refund_by_id
from saferefund.repositories.seed import SOPHIE_CUSTOMER_ID
from tests.invariants.scenario import propose_refund_awaiting_approval

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from saferefund.domain.tables import CustomerRow

CASE_ID = "case_concurrent_approval"


async def test_second_approver_cannot_reuse_a_stale_pending_status(
    seeded_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only one approval may commit, and payment must be called exactly once."""
    refund_id = await propose_refund_awaiting_approval(
        seeded_session_factory,
        case_id=CASE_ID,
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-concurrent-approval",
    )
    payment.calls.clear()

    original_lock_customer_for_update = operator_gate.lock_customer_for_update
    competing_approval_done = False

    async def lock_after_a_competing_approval_commits(
        session: AsyncSession,
        customer_id: str,
    ) -> CustomerRow:
        """Let a second approver win the lock the first approver is about to take."""
        nonlocal competing_approval_done
        if not competing_approval_done:
            competing_approval_done = True
            async with seeded_session_factory.begin() as competing_session:
                await approve_refund(
                    competing_session,
                    refund_id,
                    "operator-second",
                    session_factory=seeded_session_factory,
                )
        return await original_lock_customer_for_update(session, customer_id)

    monkeypatch.setattr(
        operator_gate,
        "lock_customer_for_update",
        lock_after_a_competing_approval_commits,
    )

    async with seeded_session_factory.begin() as session:
        first_approver_outcome = await approve_refund(
            session,
            refund_id,
            "operator-first",
            session_factory=seeded_session_factory,
        )

    assert competing_approval_done
    assert first_approver_outcome.kind is OperatorResultKind.CONFLICT

    refund_payment_calls = [
        call for call in payment.calls if call.idempotency_key == refund_id
    ]
    assert len(refund_payment_calls) == 1

    async with seeded_session_factory() as session:
        case_events = await load_case_events(session, CASE_ID)
        refund_row = await find_refund_by_id(session, refund_id)

    approved_events = [
        event for event in case_events if event.event_type is EventType.REFUND_APPROVED
    ]
    executed_events = [
        event for event in case_events if event.event_type is EventType.REFUND_EXECUTED
    ]
    assert len(approved_events) == 1
    assert len(executed_events) == 1

    assert refund_row is not None
    assert refund_row.status is RefundStatus.EXECUTED


async def test_decisive_refund_status_is_read_after_the_customer_lock(
    seeded_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The status the decision rests on must be read under the lock, not before it.

    A pre-lock read to resolve the owning case is fine; what must not happen is
    that the *only* status read precedes the lock, leaving the decision based on
    a value another transaction may already have invalidated.
    """
    refund_id = await propose_refund_awaiting_approval(
        seeded_session_factory,
        case_id=CASE_ID,
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-lock-ordering",
    )

    original_lock_customer_for_update = operator_gate.lock_customer_for_update
    original_require_refund_by_id = operator_gate.require_refund_by_id
    lock_taken = False
    refund_read_after_lock = False

    async def record_lock(
        session: AsyncSession,
        customer_id: str,
    ) -> CustomerRow:
        nonlocal lock_taken
        lock_taken = True
        return await original_lock_customer_for_update(session, customer_id)

    async def record_refund_read(session: AsyncSession, read_refund_id: str) -> object:
        nonlocal refund_read_after_lock
        if lock_taken:
            refund_read_after_lock = True
        return await original_require_refund_by_id(session, read_refund_id)

    monkeypatch.setattr(operator_gate, "lock_customer_for_update", record_lock)
    monkeypatch.setattr(operator_gate, "require_refund_by_id", record_refund_read)

    async with seeded_session_factory.begin() as session:
        await approve_refund(
            session,
            refund_id,
            "operator-ordering",
            session_factory=seeded_session_factory,
        )

    assert lock_taken
    assert refund_read_after_lock, (
        "approve_refund decided on a refund status read before the customer lock; "
        "the lock cannot protect a decision that was already made"
    )

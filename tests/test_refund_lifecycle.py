"""End-to-end refund scenarios through the gate."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from helpers import (
    get_orders_action,
    link_order_action,
    open_case,
    propose_refund_action,
)
from saferefund import adapters, ids
from saferefund.db import ORD_1001_ID, ORD_1003_ID
from saferefund.models import AuditEvent, CaseStatus, Order, Refund, RefundStatus
from saferefund.policy import Allow, RequireApproval
from saferefund.service import run_agent_action


def test_sophie_small_refund_executes(seeded_session) -> None:
    """Verified customer can list, link, and execute a refund under threshold."""
    session = seeded_session
    case = open_case(session, message_id="msg-life-1")
    run_agent_action(session, case, get_orders_action())
    run_agent_action(session, case, link_order_action())
    decision = run_agent_action(session, case, propose_refund_action("24.00"))
    session.commit()
    assert isinstance(decision, Allow)
    assert len(adapters.payment.calls) == 1
    order_row = session.get(Order, "ORD-1001")
    assert order_row is not None
    assert order_row.refunded_total == Decimal("24.00")
    assert [
        event.type
        for event in session.scalars(
            select(AuditEvent)
            .where(AuditEvent.case_id == case.id)
            .order_by(AuditEvent.id)
        )
    ] == ["orders_listed", "order_linked", "refund_executed"]


def test_large_refund_parked_for_approval(seeded_session) -> None:
    """Rule 10 parks refunds above the cumulative approval threshold."""
    session = seeded_session
    case = open_case(session, message_id="msg-life-2")
    case.linked_order_id = ORD_1003_ID
    session.flush()
    decision = run_agent_action(session, case, propose_refund_action("600.00"))
    session.commit()
    assert isinstance(decision, RequireApproval)
    assert case.status == CaseStatus.AWAITING_APPROVAL.value
    refund_row = session.scalar(select(Refund).where(Refund.case_id == case.id))
    assert refund_row is not None
    assert refund_row.status == RefundStatus.PENDING_APPROVAL.value
    assert len(adapters.payment.calls) == 0


def test_customer_total_across_orders_requires_approval(seeded_session) -> None:
    """Splitting refunds between orders cannot evade the customer threshold."""
    session = seeded_session
    first = open_case(session, message_id="msg-customer-total-1")
    first.linked_order_id = ORD_1003_ID
    session.flush()
    assert isinstance(
        run_agent_action(session, first, propose_refund_action("400.00")), Allow
    )
    second = open_case(session, message_id="msg-customer-total-2")
    second.linked_order_id = ORD_1001_ID
    session.flush()
    decision = run_agent_action(session, second, propose_refund_action("249.00"))
    assert isinstance(decision, RequireApproval)
    assert len(adapters.payment.calls) == 1


def test_consecutive_denials_bookkeeping(seeded_session) -> None:
    """Denials increment the counter; allowed actions reset it."""
    session = seeded_session
    deny_case = open_case(session, message_id="msg-life-4")
    run_agent_action(session, deny_case, propose_refund_action("10.00"))
    assert deny_case.consecutive_denials == 1

    reset_case = open_case(session, message_id="msg-life-3")
    reset_case.consecutive_denials = 2
    session.flush()
    run_agent_action(session, reset_case, get_orders_action())
    assert reset_case.consecutive_denials == 0


def test_open_refund_partial_unique_index_backstop(seeded_session) -> None:
    """Direct inserts bypass policy; the DB must reject duplicate live refunds."""
    session = seeded_session
    case_one = open_case(session, message_id="msg-backstop-1")
    case_two = open_case(session, message_id="msg-backstop-2")
    created_at = datetime(2030, 1, 15, 9, 30, tzinfo=UTC)
    session.add(
        Refund(
            id=ids.refund_id(),
            case_id=case_one.id,
            order_id=ORD_1001_ID,
            amount=Decimal("10.00"),
            status=RefundStatus.PENDING_APPROVAL.value,
            created_at=created_at,
        )
    )
    session.flush()
    session.add(
        Refund(
            id=ids.refund_id(),
            case_id=case_two.id,
            order_id=ORD_1001_ID,
            amount=Decimal("20.00"),
            status=RefundStatus.PENDING_APPROVAL.value,
            created_at=created_at,
        )
    )
    # Policy rule 8 (R_OPEN_REFUND) would deny a second propose_refund before the DB.
    with pytest.raises(IntegrityError):
        session.flush()

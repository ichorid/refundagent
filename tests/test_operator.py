"""Operator approve, reject, and pending queue."""

from datetime import UTC

from helpers import open_case, propose_refund_action, sophie_message_id
from saferefund import adapters, clock
from saferefund.db import ORD_1003_ID
from saferefund.models import CaseStatus, Order, Refund, RefundStatus
from saferefund.policy import RequireApproval
from saferefund.service import (
    approve_refund,
    list_pending_refunds,
    reject_refund,
    run_agent_action,
)


def test_list_pending_refunds(seeded_session) -> None:
    """Pending queue lists non-expired approval requests."""
    session = seeded_session
    case = open_case(session, message_id="msg-op-pending")
    case.linked_order_id = ORD_1003_ID
    session.flush()
    run_agent_action(session, case, propose_refund_action("600.00"))
    session.commit()
    pending = list_pending_refunds(session)
    assert len(pending) == 1
    assert pending[0].case_id == case.id


def test_approve_executes_refund(seeded_session) -> None:
    """One approval executes exactly its pending refund and cannot be reused."""
    session = seeded_session
    case = open_case(session, message_id="msg-op-approve")
    case.linked_order_id = ORD_1003_ID
    session.flush()
    decision = run_agent_action(session, case, propose_refund_action("600.00"))
    session.commit()
    assert isinstance(decision, RequireApproval)
    refund_id = list_pending_refunds(session)[0].id
    outcome = approve_refund(session, refund_id=refund_id, operator_id="op-1")
    session.commit()
    assert not outcome.conflict
    assert outcome.refund_status == RefundStatus.EXECUTED.value
    assert len(adapters.payment.calls) == 1
    refund_row = session.get(Refund, refund_id)
    assert refund_row is not None
    assert refund_row.status == RefundStatus.EXECUTED.value
    assert adapters.payment.calls[0].idempotency_key == refund_id
    assert adapters.payment.calls[0].amount == refund_row.amount == 600
    order = session.get(Order, refund_row.order_id)
    assert order is not None
    assert order.refunded_total == 600
    assert case.status == CaseStatus.OPEN.value
    second = approve_refund(session, refund_id=refund_id, operator_id="op-2")
    assert second.conflict
    assert second.refund_status == RefundStatus.EXECUTED.value
    assert len(adapters.payment.calls) == 1


def test_reject_refund_reopens_case(seeded_session) -> None:
    """Reject marks the refund rejected and reopens the case."""
    session = seeded_session
    case = open_case(session, message_id="msg-op-reject")
    case.linked_order_id = ORD_1003_ID
    session.flush()
    run_agent_action(session, case, propose_refund_action("600.00"))
    session.commit()
    refund_id = list_pending_refunds(session)[0].id
    outcome = reject_refund(
        session, refund_id=refund_id, operator_id="op-1", reason="Not eligible"
    )
    session.commit()
    assert outcome.refund_status == RefundStatus.REJECTED.value
    assert case.status == CaseStatus.OPEN.value
    assert len(adapters.payment.calls) == 0


def test_operator_approve_conflict(seeded_session, client) -> None:
    """Approving a non-pending refund returns HTTP 409."""
    session = seeded_session
    case = open_case(session, message_id=sophie_message_id())
    case.linked_order_id = ORD_1003_ID
    session.flush()
    run_agent_action(session, case, propose_refund_action("600.00"))
    session.commit()
    refund_id = client.get("/operator/pending").json()["pending_refunds"][0][
        "refund_id"
    ]
    first = client.post(
        "/operator/approve",
        json={"refund_id": refund_id, "operator_id": "op-1"},
    )
    assert first.status_code == 200
    second = client.post(
        "/operator/approve",
        json={"refund_id": refund_id, "operator_id": "op-1"},
    )
    assert second.status_code == 409


def test_approval_at_expiry_is_expired_not_executed(seeded_session) -> None:
    """The expiry boundary cannot leave a hidden refund approvable."""
    session = seeded_session
    case = open_case(session, message_id="msg-op-expiry")
    case.linked_order_id = ORD_1003_ID
    session.flush()
    run_agent_action(session, case, propose_refund_action("600.00"))
    pending = list_pending_refunds(session)[0]
    assert pending.approval_expires_at is not None
    clock.set_now_for_tests(pending.approval_expires_at.replace(tzinfo=UTC))
    outcome = approve_refund(session, refund_id=pending.id, operator_id="op-1")
    assert outcome.conflict
    assert outcome.refund_status == RefundStatus.EXPIRED.value
    assert len(adapters.payment.calls) == 0

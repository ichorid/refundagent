"""Audit rows are written and readable."""

from sqlalchemy import select

from helpers import get_orders_action, open_case
from saferefund.agent import build_prompt
from saferefund.models import AuditEvent
from saferefund.service import audit, run_agent_action


def test_audit_written_on_action(seeded_session) -> None:
    """Allowed actions append informational audit rows."""
    session = seeded_session
    case = open_case(session, message_id="msg-audit-1")
    run_agent_action(session, case, get_orders_action())
    session.commit()
    events = session.scalars(
        select(AuditEvent).where(AuditEvent.case_id == case.id)
    ).all()
    assert any(event.type == "orders_listed" for event in events)


def test_audit_helper_stores_detail(seeded_session) -> None:
    """Audit detail JSON is persisted and readable from the database."""
    session = seeded_session
    case = open_case(session, message_id="msg-audit-2")
    audit(session, case=case, type="test_marker", note="visible")
    session.commit()
    event = session.scalar(
        select(AuditEvent).where(
            AuditEvent.case_id == case.id,
            AuditEvent.type == "test_marker",
        )
    )
    assert event is not None
    assert event.detail["note"] == "visible"


def test_prompt_reply_state_comes_from_case_not_audit(seeded_session) -> None:
    """Free-form audit rows cannot change the control state shown to the model."""
    session = seeded_session
    case = open_case(session, message_id="msg-audit-control")
    audit(session, case=case, type="reply_sent")
    assert "reply_sent_after_last_refund: false" in build_prompt(session, case)

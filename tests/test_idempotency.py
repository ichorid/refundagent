"""Customer-scoped inbound message idempotency."""

from sqlalchemy import func, select

from helpers import sophie_message_id
from saferefund.db import SOPHIE_EMAIL
from saferefund.models import Case
from saferefund.service import handle_inbound_email


def test_duplicate_inbound_returns_same_case(seeded_session) -> None:
    """The same message id for one customer reopens the existing case."""
    session = seeded_session
    message_id = sophie_message_id()
    first = handle_inbound_email(
        session,
        envelope_from=SOPHIE_EMAIL,
        message_id=message_id,
        subject="Refund",
        body="First",
    )
    session.commit()
    second = handle_inbound_email(
        session,
        envelope_from=SOPHIE_EMAIL,
        message_id=message_id,
        subject="Refund",
        body="Second",
    )
    assert first.case_id == second.case_id
    count = session.scalar(select(func.count()).select_from(Case))
    assert count == 1


def test_idempotency_is_customer_scoped(seeded_session) -> None:
    """The same message id may exist for different customers."""
    session = seeded_session
    message_id = "msg-shared-id"
    sophie = handle_inbound_email(
        session,
        envelope_from=SOPHIE_EMAIL,
        message_id=message_id,
        subject="Hi",
        body="Sophie",
    )
    tom = handle_inbound_email(
        session,
        envelope_from="tom@example.com",
        message_id=message_id,
        subject="Hi",
        body="Tom",
    )
    session.commit()
    assert sophie.case_id != tom.case_id
    count = session.scalar(select(func.count()).select_from(Case))
    assert count == 2

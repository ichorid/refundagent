"""Verification request, confirm, and expiry."""

from datetime import UTC, timedelta

from sqlalchemy import select

from helpers import (
    get_orders_action,
    open_case,
    request_verification_action,
    tom_message_id,
)
from saferefund import clock, config
from saferefund.db import TOM_CUSTOMER_ID
from saferefund.models import CaseStatus, Customer, VerificationToken
from saferefund.policy import Deny
from saferefund.service import confirm_verification, run_agent_action


def test_unverified_customer_denied_get_orders(seeded_session) -> None:
    """Rule 3 blocks order access until verification completes."""
    session = seeded_session
    case = open_case(session, customer_id=TOM_CUSTOMER_ID, message_id=tom_message_id())
    decision = run_agent_action(session, case, get_orders_action())
    assert isinstance(decision, Deny)
    assert decision.rule == "R_UNVERIFIED"


def test_verification_request_and_confirm(seeded_session) -> None:
    """Request sends a token; confirm marks the customer verified."""
    session = seeded_session
    case = open_case(session, customer_id=TOM_CUSTOMER_ID, message_id=tom_message_id())
    run_agent_action(session, case, request_verification_action())
    session.commit()
    token_row = session.scalar(
        select(VerificationToken).where(VerificationToken.case_id == case.id)
    )
    assert token_row is not None
    assert case.status == CaseStatus.AWAITING_VERIFICATION.value
    outcome = confirm_verification(session, token=token_row.token)
    session.commit()
    assert outcome.found and not outcome.expired
    customer = session.get(Customer, TOM_CUSTOMER_ID)
    assert customer is not None
    assert customer.verified


def test_verification_token_expired(seeded_session) -> None:
    """Expired tokens are rejected without verifying the customer."""
    session = seeded_session
    case = open_case(
        session, customer_id=TOM_CUSTOMER_ID, message_id="msg-expired-vtok"
    )
    run_agent_action(session, case, request_verification_action())
    session.commit()
    token_row = session.scalar(
        select(VerificationToken).where(VerificationToken.case_id == case.id)
    )
    assert token_row is not None
    clock.set_now_for_tests(
        token_row.expires_at.replace(tzinfo=UTC)
        + timedelta(seconds=config.VERIFICATION_TTL_SECONDS)
    )
    outcome = confirm_verification(session, token=token_row.token)
    assert outcome.expired
    customer = session.get(Customer, TOM_CUSTOMER_ID)
    assert customer is not None
    assert not customer.verified

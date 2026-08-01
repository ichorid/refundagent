"""Ownership checks and adapter mediation through the gate."""

from decimal import Decimal
from pathlib import Path

from helpers import (
    customer_email,
    link_order_action,
    open_case,
    propose_refund_action,
    send_reply_action,
)
from saferefund import adapters
from saferefund.db import ORD_2001_ID, SOPHIE_CUSTOMER_ID
from saferefund.policy import Deny
from saferefund.service import run_agent_action


def test_link_order_denies_not_owned(seeded_session) -> None:
    """Rule 5 blocks linking another customer's order."""
    session = seeded_session
    case = open_case(session, customer_id=SOPHIE_CUSTOMER_ID, message_id="msg-own-1")
    decision = run_agent_action(session, case, link_order_action(ORD_2001_ID))
    assert isinstance(decision, Deny)
    assert decision.rule == "R_NOT_OWNED"
    assert case.linked_order_id is None


def test_mailer_recipient_is_customer_email(seeded_session) -> None:
    """send_reply never passes a model-supplied recipient to the mailer."""
    session = seeded_session
    case = open_case(session, message_id="msg-mail-1")
    run_agent_action(
        session,
        case,
        send_reply_action("Hello", "We are reviewing your case."),
    )
    session.commit()
    assert len(adapters.mailer.outbox) == 1
    assert adapters.mailer.outbox[0].to == customer_email(session, SOPHIE_CUSTOMER_ID)


def test_payment_adapter_only_called_from_gate(seeded_session) -> None:
    """Refund payment runs only after policy Allow through service."""
    session = seeded_session
    case = open_case(session, message_id="msg-pay-1")
    case.linked_order_id = "ORD-1001"
    session.flush()
    run_agent_action(session, case, propose_refund_action("10.00"))
    session.commit()
    assert len(adapters.payment.calls) == 1
    assert adapters.payment.calls[0].amount == Decimal("10.00")


def test_only_service_invokes_external_effect_methods() -> None:
    """Every production adapter invocation belongs to the policy gate module."""
    package = Path(__file__).parents[1] / "src" / "saferefund"
    effect_calls = ("payment.refund(", "mailer.send(", "ticketing.escalate(")
    callers = {
        path.name
        for path in package.glob("*.py")
        if any(call in path.read_text() for call in effect_calls)
    }
    assert callers == {"service.py"}

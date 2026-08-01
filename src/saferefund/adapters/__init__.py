"""Mock external-system adapters for payment, mail, and ticketing."""

from . import mailer, payment, ticketing
from .mailer import OutboxMessage, reset_mailer_for_tests
from .payment import (
    RefundCall,
    RefundResult,
    reset_payment_for_tests,
)
from .ticketing import EscalationRecord, reset_ticketing_for_tests


def reset_adapters_for_tests() -> None:
    """Reset every mock adapter to a clean state."""
    reset_payment_for_tests()
    reset_mailer_for_tests()
    reset_ticketing_for_tests()


__all__ = [
    "EscalationRecord",
    "OutboxMessage",
    "RefundCall",
    "RefundResult",
    "mailer",
    "payment",
    "reset_adapters_for_tests",
    "reset_mailer_for_tests",
    "reset_payment_for_tests",
    "reset_ticketing_for_tests",
    "ticketing",
]

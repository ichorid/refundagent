"""Application-owned identifiers with deterministic test sequencing."""

from itertools import count
from typing import Final


class _IdentifierSequence:
    def __init__(self) -> None:
        self.counter = count(1)

    def next_value(self) -> int:
        return next(self.counter)

    def reset(self, start_at: int) -> None:
        self.counter = count(start_at)


_identifier_sequence = _IdentifierSequence()
_CASE_PREFIX: Final = "case_"
_REFUND_PREFIX: Final = "rfnd_"
_EVENT_PREFIX: Final = "evt_"
_TICKET_PREFIX: Final = "tkt_"
_VERIFICATION_TOKEN_PREFIX: Final = "vtok_"


def _next_identifier(prefix: str) -> str:
    return f"{prefix}{_identifier_sequence.next_value()}"


def case_id() -> str:
    """Create a case identifier."""
    return _next_identifier(_CASE_PREFIX)


def refund_id() -> str:
    """Create a refund identifier."""
    return _next_identifier(_REFUND_PREFIX)


def event_id() -> str:
    """Create an event identifier."""
    return _next_identifier(_EVENT_PREFIX)


def ticket_id() -> str:
    """Create a ticket identifier."""
    return _next_identifier(_TICKET_PREFIX)


def verification_token() -> str:
    """Create a verification token."""
    return _next_identifier(_VERIFICATION_TOKEN_PREFIX)


def reset_counter_for_tests(*, start_at: int = 1) -> None:
    """Reset identifier generation to a deterministic positive sequence."""
    if start_at < 1:
        message = "Identifier counter must start at one or greater."
        raise ValueError(message)
    _identifier_sequence.reset(start_at)

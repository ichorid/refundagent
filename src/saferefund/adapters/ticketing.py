"""Mock ticketing adapter with deterministic ticket identifiers."""

from dataclasses import dataclass

from saferefund import ids


@dataclass(frozen=True, slots=True)
class EscalationRecord:
    """One escalation submitted to the mock ticketing system."""

    reason: str
    ticket_id: str


class _TicketingState:
    def __init__(self) -> None:
        self.escalations: list[EscalationRecord] = []


_ticketing_state = _TicketingState()

escalations = _ticketing_state.escalations


def escalate(*, reason: str) -> str:
    """Create a mock ticket and return its identifier."""
    ticket_id = ids.ticket_id()
    _ticketing_state.escalations.append(
        EscalationRecord(reason=reason, ticket_id=ticket_id),
    )
    return ticket_id


def reset_ticketing_for_tests() -> None:
    """Clear recorded escalations between tests."""
    _ticketing_state.escalations.clear()

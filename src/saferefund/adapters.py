"""Mock external-system adapters for payment, mail, and ticketing."""

from dataclasses import dataclass
from decimal import Decimal

from saferefund import ids


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    """One message queued by the mock mailer."""

    to: str
    subject: str
    body: str


@dataclass(frozen=True, slots=True)
class RefundCall:
    """One recorded invocation of the mock payment provider."""

    idempotency_key: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class RefundResult:
    """Successful refund outcome from the mock provider."""

    ok: bool
    provider_ref: str


@dataclass(frozen=True, slots=True)
class EscalationRecord:
    """One escalation submitted to the mock ticketing system."""

    reason: str
    ticket_id: str


class MailerAdapter:
    """In-memory mailer with an inspectable outbox."""

    def __init__(self) -> None:
        """Initialize an empty outbox."""
        self.outbox: list[OutboxMessage] = []

    def send(self, *, to: str, subject: str, body: str) -> None:
        """Append a message to the inspectable outbox."""
        self.outbox.append(OutboxMessage(to=to, subject=subject, body=body))

    def reset(self) -> None:
        """Clear the outbox between tests."""
        self.outbox.clear()


class PaymentAdapter:
    """Mock payment provider with refund-id idempotency."""

    def __init__(self) -> None:
        """Initialize empty call history and idempotency state."""
        self.calls: list[RefundCall] = []
        self._provider_ref_by_idempotency_key: dict[str, str] = {}

    def refund(self, *, idempotency_key: str, amount: Decimal) -> RefundResult:
        """Execute a mock refund; repeated keys return the same provider reference."""
        self.calls.append(RefundCall(idempotency_key=idempotency_key, amount=amount))
        existing_provider_ref = self._provider_ref_by_idempotency_key.get(
            idempotency_key,
        )
        if existing_provider_ref is not None:
            return RefundResult(ok=True, provider_ref=existing_provider_ref)

        provider_ref = f"pay_{idempotency_key}"
        self._provider_ref_by_idempotency_key[idempotency_key] = provider_ref
        return RefundResult(ok=True, provider_ref=provider_ref)

    def reset(self) -> None:
        """Clear recorded calls and idempotency state between tests."""
        self.calls.clear()
        self._provider_ref_by_idempotency_key.clear()


class TicketingAdapter:
    """Mock ticketing system with deterministic ticket identifiers."""

    def __init__(self) -> None:
        """Initialize an empty escalation log."""
        self.escalations: list[EscalationRecord] = []

    def escalate(self, *, reason: str) -> str:
        """Create a mock ticket and return its identifier."""
        ticket = ids.ticket_id()
        self.escalations.append(EscalationRecord(reason=reason, ticket_id=ticket))
        return ticket

    def reset(self) -> None:
        """Clear recorded escalations between tests."""
        self.escalations.clear()


mailer = MailerAdapter()
payment = PaymentAdapter()
ticketing = TicketingAdapter()


def reset_adapters() -> None:
    """Reset every mock adapter to a clean state."""
    mailer.reset()
    payment.reset()
    ticketing.reset()

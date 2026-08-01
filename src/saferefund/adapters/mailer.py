"""Mock mailer with an in-memory outbox for test and demo inspection."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    """One message queued by the mock mailer."""

    to: str
    subject: str
    body: str


class _MailerState:
    def __init__(self) -> None:
        self.outbox: list[OutboxMessage] = []


_mailer_state = _MailerState()

outbox = _mailer_state.outbox


def send(*, to: str, subject: str, body: str) -> None:
    """Append a message to the inspectable outbox."""
    _mailer_state.outbox.append(
        OutboxMessage(to=to, subject=subject, body=body),
    )


def snapshot_outbox() -> tuple[OutboxMessage, ...]:
    """Return a read-only snapshot of queued messages for demo and test inspection."""
    return tuple(_mailer_state.outbox)


def reset_mailer_for_tests() -> None:
    """Clear the outbox between tests."""
    _mailer_state.outbox.clear()

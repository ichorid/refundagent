"""Readable ASCII tables for demo event and mailer output."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence


class OutboxRenderable(Protocol):
    """Minimal mailer message shape for table rendering."""

    @property
    def to(self) -> str:
        """Message recipient."""
        ...

    @property
    def subject(self) -> str:
        """Message subject."""
        ...

    @property
    def body(self) -> str:
        """Message body."""
        ...


def format_event_table(
    *,
    case_id: str,
    status: str,
    events: list[dict[str, object]],
) -> str:
    """Render one case's event sequence as a fixed-width table."""
    header_lines = [
        f"Case: {case_id}",
        f"Status: {status}",
        "",
        _table_row("seq", "type", "actor", "channel"),
        _table_row("---", "----------------------", "--------", "-------------"),
    ]
    body_lines = [
        _table_row(
            str(event["seq"]),
            str(event["type"]),
            str(event["actor"]),
            str(event["channel"]),
        )
        for event in events
    ]
    return "\n".join(header_lines + body_lines)


def format_outbox_table(messages: Sequence[OutboxRenderable]) -> str:
    """Render the mailer outbox as a fixed-width table."""
    header_lines = [
        "Mailer outbox",
        "",
        _table_row("#", "to", "subject", "body"),
        _table_row("-", "----------------------", "----------------------", "-----"),
    ]
    if not messages:
        header_lines.append(_table_row("-", "(empty)", "", ""))
        return "\n".join(header_lines)

    body_lines = [
        _table_row(
            str(index),
            message.to,
            message.subject,
            _truncate(message.body, width=40),
        )
        for index, message in enumerate(messages, start=1)
    ]
    return "\n".join(header_lines + body_lines)


def _table_row(
    first: str,
    second: str,
    third: str,
    fourth: str,
) -> str:
    return f"{first:>4}  {second:<22}  {third:<8}  {fourth}"


def _truncate(text: str, *, width: int) -> str:
    if len(text) <= width:
        return text
    return f"{text[: width - 3]}..."

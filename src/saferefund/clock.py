"""The single UTC time source, with a test-controlled override."""

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(slots=True)
class _ClockState:
    override_now: datetime | None = None


_clock_state = _ClockState()


def now() -> datetime:
    """Return the configured UTC instant or the current UTC instant."""
    if _clock_state.override_now is not None:
        return _clock_state.override_now
    return datetime.now(UTC)


def set_now_for_tests(override_now: datetime) -> None:
    """Install a UTC-aware instant for deterministic tests."""
    if override_now.tzinfo is None or override_now.utcoffset() is None:
        message = "Test clock overrides must be timezone-aware."
        raise ValueError(message)
    _clock_state.override_now = override_now.astimezone(UTC)


def reset_now_for_tests() -> None:
    """Restore the live UTC clock after a test."""
    _clock_state.override_now = None

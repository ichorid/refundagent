"""Policy verdict values and the continue sentinel."""

from dataclasses import dataclass
from enum import Enum, auto
from typing import Final


class Continue(Enum):
    """Sentinel returned when a check passes and evaluation should continue."""

    TOKEN = auto()


CONTINUE: Final = Continue.TOKEN


@dataclass(frozen=True, slots=True)
class Allow:
    """The proposed action is permitted without operator involvement."""


@dataclass(frozen=True, slots=True)
class Deny:
    """The proposed action is refused."""

    rule: str
    agent_reason: str
    customer_reason: str


@dataclass(frozen=True, slots=True)
class RequireApproval:
    """The proposal is accepted but payment awaits operator approval."""

    rule: str
    reason: str


@dataclass(frozen=True, slots=True)
class ForceEscalate:
    """Repeated denials require human escalation."""

    rule: str
    reason: str


type Verdict = Allow | Deny | RequireApproval | ForceEscalate
type CheckResult = Continue | Deny | RequireApproval | ForceEscalate


__all__ = [
    "CONTINUE",
    "Allow",
    "CheckResult",
    "Continue",
    "Deny",
    "ForceEscalate",
    "RequireApproval",
    "Verdict",
]

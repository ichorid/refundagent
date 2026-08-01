"""Result types returned by operator and verification gate operations."""

from dataclasses import dataclass
from enum import StrEnum

from saferefund.domain.enums import RefundStatus


class OperatorResultKind(StrEnum):
    """Whether an operator refund action succeeded or conflicted."""

    APPROVED = "approved"
    REJECTED = "rejected"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class OperatorOutcome:
    """Operator approve/reject result with case and refund identifiers for routes."""

    kind: OperatorResultKind
    case_id: str
    refund_id: str
    refund_status: RefundStatus
    reopened_case_ids: tuple[str, ...] = ()


class VerificationResultKind(StrEnum):
    """Whether a verification token was confirmed, expired, or unknown."""

    NOT_FOUND = "not_found"
    EXPIRED = "expired"
    VERIFIED = "verified"


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    """Verification confirm result with case ids routes may resume."""

    kind: VerificationResultKind
    customer_id: str | None = None
    issuing_case_id: str | None = None
    open_case_ids: tuple[str, ...] = ()

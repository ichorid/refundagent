"""Closed vocabularies shared by persistence, events, and projections."""

from enum import StrEnum


class Actor(StrEnum):
    """Trusted category accountable for an event."""

    CUSTOMER = "customer"
    AGENT = "agent"
    OPERATOR = "operator"
    SYSTEM = "system"


class Channel(StrEnum):
    """Trusted entry channel that produced an event."""

    EMAIL = "email"
    OPERATOR_API = "operator_api"
    VERIFICATION_API = "verification_api"
    INTERNAL = "internal"


class CaseStatus(StrEnum):
    """Lifecycle state folded from a case event stream."""

    OPEN = "open"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_VERIFICATION = "awaiting_verification"
    CLOSED = "closed"


class RefundStatus(StrEnum):
    """Lifecycle state of the mutable refund enforcement row."""

    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    EXECUTED = "executed"
    REJECTED = "rejected"
    EXPIRED = "expired"


class EscalationOrigin(StrEnum):
    """Source that required a case to be escalated."""

    AGENT = "agent"
    POLICY = "policy"
    STEP_LIMIT = "step_limit"
    PARSE_LIMIT = "parse_limit"
    MODEL_FAILURE = "model_failure"


class OrderStatus(StrEnum):
    """Advisory seed status shown to the model."""

    DELIVERED = "delivered"
    DELIVERED_DAMAGED = "delivered_damaged"
    SHIPPED = "shipped"


class CaseOutcome(StrEnum):
    """Terminal outcome recorded when a case closes."""

    FINISHED = "finished"
    ESCALATED = "escalated"
    STEP_LIMIT = "step_limit"
    PARSE_LIMIT = "parse_limit"
    MODEL_FAILURE = "model_failure"


class VerificationMethod(StrEnum):
    """Trusted pathway that established customer verification."""

    SEED = "seed"
    TOKEN = "token"

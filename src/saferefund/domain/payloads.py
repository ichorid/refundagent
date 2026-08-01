"""Pydantic payload schemas for every catalogue event type."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from saferefund.domain.enums import CaseOutcome, EscalationOrigin, VerificationMethod


class _EventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaseOpenedPayload(_EventPayload):
    """Opening email dedupe key for a new case."""

    opening_message_id: str


class EmailReceivedPayload(_EventPayload):
    """Inbound customer email stored as untrusted free text."""

    message_id: str
    subject: str
    body: str


class OrdersListedPayload(_EventPayload):
    """Order identifiers exposed to the agent for one case."""

    order_ids: list[str]


class OrderLinkedPayload(_EventPayload):
    """Validated order ownership link for a case."""

    order_id: str


class VerificationRequestedPayload(_EventPayload):
    """Verification token issued for a case."""

    token: str
    expires_at: datetime


class CustomerVerifiedPayload(_EventPayload):
    """Trusted verification fact for a customer."""

    method: VerificationMethod


class RefundProposedPayload(_EventPayload):
    """Refund intent recorded before approval routing."""

    refund_id: str
    amount: Decimal


class RefundAutoApprovedPayload(_EventPayload):
    """System auto-approval of a proposed refund."""

    refund_id: str


class RefundApprovalRequiredPayload(_EventPayload):
    """Refund proposal held for operator approval."""

    refund_id: str
    amount: Decimal
    rule: str


class RefundApprovedPayload(_EventPayload):
    """Operator approval of a pending refund."""

    refund_id: str
    operator_id: str


class RefundRejectedPayload(_EventPayload):
    """Operator rejection of a pending refund."""

    refund_id: str
    operator_id: str
    reason: str


class RefundExpiredPayload(_EventPayload):
    """Pending refund that passed its approval TTL."""

    refund_id: str


class RefundExecutedPayload(_EventPayload):
    """Payment execution evidence for a refund."""

    refund_id: str
    amount: Decimal
    provider_ref: str


class ReplySentPayload(_EventPayload):
    """Agent-authored outbound email stored as untrusted free text."""

    subject: str
    body: str


class EscalatedPayload(_EventPayload):
    """Human escalation created for a case."""

    reason: str
    origin: EscalationOrigin
    ticket_id: str


class ActionDeniedPayload(_EventPayload):
    """Policy denial with separate agent and customer messaging."""

    action: str
    rule: str
    agent_reason: str
    customer_reason: str


class InvalidOutputPayload(_EventPayload):
    """Unparseable model output retained for audit only."""

    preview: str
    byte_count: int
    sha256: str
    error: str


class CaseClosedPayload(_EventPayload):
    """Terminal case outcome and optional finish summary."""

    outcome: CaseOutcome
    summary: str | None

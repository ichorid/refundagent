"""HTTP request and response models for the public API boundary."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from saferefund import config
from saferefund.domain.enums import CaseStatus, RefundStatus


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InboundEmailRequest(_StrictRequest):
    """Transport envelope and untrusted email content for one inbound message."""

    envelope_from: str = Field(
        min_length=1,
        max_length=config.HTTP_ENVELOPE_FROM_MAX_LENGTH,
    )
    message_id: str = Field(
        min_length=1,
        max_length=config.HTTP_MESSAGE_ID_MAX_LENGTH,
    )
    subject: str = Field(
        min_length=1,
        max_length=config.HTTP_INBOUND_SUBJECT_MAX_LENGTH,
    )
    body: str = Field(
        min_length=1,
        max_length=config.HTTP_INBOUND_BODY_MAX_LENGTH,
    )


class CaseEventResponse(BaseModel):
    """One case-scoped event in ascending sequence order."""

    seq: int
    type: str
    actor: str
    channel: str


class InboundEmailResponse(BaseModel):
    """Case state after inbound routing and any agent loop run."""

    case_id: str
    status: CaseStatus
    events: list[CaseEventResponse]


class UnknownSenderResponse(BaseModel):
    """Acknowledgement for an email from an unrecognised sender."""

    handled: str = Field(default="unknown_sender")


class OperatorApproveRequest(_StrictRequest):
    """Operator approval of one pending refund."""

    refund_id: str = Field(
        min_length=1,
        max_length=config.HTTP_REFUND_ID_MAX_LENGTH,
    )
    operator_id: str = Field(
        min_length=1,
        max_length=config.HTTP_OPERATOR_ID_MAX_LENGTH,
    )


class OperatorRejectRequest(_StrictRequest):
    """Operator rejection of one pending refund."""

    refund_id: str = Field(
        min_length=1,
        max_length=config.HTTP_REFUND_ID_MAX_LENGTH,
    )
    operator_id: str = Field(
        min_length=1,
        max_length=config.HTTP_OPERATOR_ID_MAX_LENGTH,
    )
    reason: str = Field(
        min_length=1,
        max_length=config.HTTP_OPERATOR_REASON_MAX_LENGTH,
    )


class OperatorActionResponse(BaseModel):
    """Successful operator refund decision."""

    case_id: str
    refund_id: str
    refund_status: RefundStatus


class OperatorConflictResponse(BaseModel):
    """Refund was not pending approval when the operator acted."""

    refund_id: str
    refund_status: RefundStatus


class OperatorPendingRefundResponse(BaseModel):
    """One refund awaiting operator approval within its TTL window."""

    refund_id: str
    case_id: str
    order_id: str
    amount: Decimal
    approval_expires_at: datetime


class OperatorPendingResponse(BaseModel):
    """Active pending refunds visible to the operator queue."""

    pending_refunds: list[OperatorPendingRefundResponse]


class VerificationConfirmRequest(_StrictRequest):
    """Customer verification token presented out of band."""

    token: str = Field(
        min_length=1,
        max_length=config.HTTP_VERIFICATION_TOKEN_MAX_LENGTH,
    )


class VerificationConfirmResponse(BaseModel):
    """Customer verification succeeded and open cases were resumed."""

    customer_id: str
    resumed_case_ids: list[str]


class VerificationExpiredResponse(BaseModel):
    """Verification token was recognised but no longer valid."""

    customer_id: str
    issuing_case_id: str

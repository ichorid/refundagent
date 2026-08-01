"""Typed agent proposals with structural capability restrictions."""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

_ACTION_TEXT_FIELD_MAX_LENGTH = 4000
_ACTION_ORDER_ID_MAX_LENGTH = 64


class _ActionBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GetOrders(_ActionBase):
    """List the current customer's orders."""

    action: Literal["get_orders"]


class LinkOrder(_ActionBase):
    """Attach a validated order to the active case."""

    action: Literal["link_order"]
    order_id: str = Field(min_length=1, max_length=_ACTION_ORDER_ID_MAX_LENGTH)


class ProposeRefund(_ActionBase):
    """Propose a refund against the case's linked order."""

    action: Literal["propose_refund"]
    amount: Decimal


class SendReply(_ActionBase):
    """Send a customer-facing email reply for the active case."""

    action: Literal["send_reply"]
    subject: str = Field(min_length=1, max_length=_ACTION_TEXT_FIELD_MAX_LENGTH)
    body: str = Field(min_length=1, max_length=_ACTION_TEXT_FIELD_MAX_LENGTH)


class RequestVerification(_ActionBase):
    """Request out-of-band customer verification."""

    action: Literal["request_verification"]


class Escalate(_ActionBase):
    """Escalate the case to human support."""

    action: Literal["escalate"]
    reason: str = Field(min_length=1, max_length=_ACTION_TEXT_FIELD_MAX_LENGTH)


class Finish(_ActionBase):
    """Close the case with a terminal summary."""

    action: Literal["finish"]
    summary: str = Field(min_length=1, max_length=_ACTION_TEXT_FIELD_MAX_LENGTH)


type Action = Annotated[
    GetOrders
    | LinkOrder
    | ProposeRefund
    | SendReply
    | RequestVerification
    | Escalate
    | Finish,
    Field(discriminator="action"),
]

ACTION_MODEL_CLASSES: tuple[type[_ActionBase], ...] = (
    GetOrders,
    LinkOrder,
    ProposeRefund,
    SendReply,
    RequestVerification,
    Escalate,
    Finish,
)

__all__ = [
    "ACTION_MODEL_CLASSES",
    "Action",
    "Escalate",
    "Finish",
    "GetOrders",
    "LinkOrder",
    "ProposeRefund",
    "RequestVerification",
    "SendReply",
]

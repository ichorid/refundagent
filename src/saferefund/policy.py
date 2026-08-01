"""Deterministic refund policy for one proposed action."""

from dataclasses import dataclass
from decimal import Decimal
from typing import assert_never

from saferefund.actions import (
    Action,
    Finish,
    GetOrders,
    LinkOrder,
    ProposeRefund,
    RequestVerification,
    SendReply,
)
from saferefund.actions import (
    Escalate as EscalateAction,
)
from saferefund.models import CaseStatus

_MAX_MONEY_DECIMAL_SCALE = -2


@dataclass(frozen=True, slots=True)
class PolicyState:
    """Everything a rule may read. Built by service.py from database rows."""

    case_status: CaseStatus
    consecutive_denials: int
    customer_verified: bool
    owned_order_ids: frozenset[str]
    linked_order_id: str | None
    linked_order_total: Decimal | None
    linked_order_refunded: Decimal | None
    linked_order_has_open_refund: bool
    customer_refunded_total: Decimal
    approval_threshold: Decimal
    denial_loop_threshold: int


@dataclass(frozen=True, slots=True)
class Allow:
    """Action may proceed without operator involvement."""


@dataclass(frozen=True, slots=True)
class Deny:
    """Policy denied the proposed action."""

    rule: str
    agent_reason: str
    customer_reason: str


@dataclass(frozen=True, slots=True)
class RequireApproval:
    """Refund exceeds threshold and needs operator approval."""

    rule: str
    reason: str


@dataclass(frozen=True, slots=True)
class Escalate:
    """Case must be escalated to a human."""

    rule: str
    reason: str


Decision = Allow | Deny | RequireApproval | Escalate


def decide(state: PolicyState, action: Action) -> Decision:
    """Return the first decisive verdict for one proposed action."""
    if state.case_status is not CaseStatus.OPEN:
        return Deny(
            rule="R_CASE_NOT_OPEN",
            agent_reason="This case is not open for agent actions.",
            customer_reason=(
                "We cannot process this request while your case is "
                "waiting on another step."
            ),
        )
    if state.consecutive_denials >= state.denial_loop_threshold:
        return Escalate(
            rule="R_DENIAL_LOOP",
            reason="The agent received too many consecutive denials and must escalate.",
        )
    if (
        isinstance(action, (GetOrders, LinkOrder, ProposeRefund))
        and not state.customer_verified
    ):
        return Deny(
            rule="R_UNVERIFIED",
            agent_reason=(
                "The customer is not verified. Use request_verification first."
            ),
            customer_reason=(
                "Please verify your identity before we can access orders or refunds."
            ),
        )
    if isinstance(action, RequestVerification) and state.customer_verified:
        return Deny(
            rule="R_ALREADY_VERIFIED",
            agent_reason="The customer is already verified.",
            customer_reason="Your identity has already been verified.",
        )
    if isinstance(action, LinkOrder) and action.order_id not in state.owned_order_ids:
        return Deny(
            rule="R_NOT_OWNED",
            agent_reason=f"Order {action.order_id} does not belong to this customer.",
            customer_reason="We could not find that order on your account.",
        )
    if isinstance(action, ProposeRefund):
        if state.linked_order_id is None:
            return Deny(
                rule="R_NO_LINKED_ORDER",
                agent_reason="Link an order to this case before proposing a refund.",
                customer_reason=(
                    "We need to identify the order before processing a refund."
                ),
            )
        amount = action.amount
        invalid_amount = Deny(
            rule="R_AMOUNT",
            agent_reason=(
                "Refund amount must be a finite positive value with at most "
                "two decimals."
            ),
            customer_reason="The refund amount is not valid.",
        )
        if not amount.is_finite() or amount <= 0:
            return invalid_amount
        scale = amount.as_tuple().exponent
        if isinstance(scale, int) and scale < _MAX_MONEY_DECIMAL_SCALE:
            return invalid_amount
        if state.linked_order_has_open_refund:
            return Deny(
                rule="R_OPEN_REFUND",
                agent_reason="This order already has an open refund request.",
                customer_reason="There is already a refund in progress for this order.",
            )
        total, refunded = state.linked_order_total, state.linked_order_refunded
        if total is not None and refunded is not None:
            remainder = total - refunded
            if amount > remainder:
                return Deny(
                    rule="R_REMAINDER",
                    agent_reason=(
                        f"Refund amount {amount} exceeds the refundable remainder "
                        f"{remainder}."
                    ),
                    customer_reason=(
                        "The requested refund is larger than the remaining "
                        "refundable amount."
                    ),
                )
            if state.customer_refunded_total + amount > state.approval_threshold:
                cumulative = state.customer_refunded_total + amount
                return RequireApproval(
                    rule="R_THRESHOLD",
                    reason=(
                        f"Cumulative refunds of {cumulative} exceed threshold "
                        f"{state.approval_threshold}."
                    ),
                )
    # send_reply: constrained only by rules 1-2 (recipient is derived, not governed).
    # escalate: constrained only by rules 1-2 (hands case to a human).
    # finish: constrained only by rules 1-2 (terminal close, summary is untrusted).
    match action:
        case (
            GetOrders()
            | LinkOrder()
            | ProposeRefund()
            | SendReply()
            | RequestVerification()
            | EscalateAction()
            | Finish()
        ):
            return Allow()
        case _:  # pragma: no cover - unreachable while the match above is exhaustive
            assert_never(action)

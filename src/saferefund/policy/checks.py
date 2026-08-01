"""Pure policy predicates with stable rule identifiers."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from saferefund.actions.models import (
    Action,
    Escalate,
    Finish,
    GetOrders,
    LinkOrder,
    ProposeRefund,
    RequestVerification,
    SendReply,
    _ActionBase,
)
from saferefund.domain.enums import CaseStatus
from saferefund.policy.context import RuleContext
from saferefund.policy.verdicts import (
    CONTINUE,
    CheckResult,
    Deny,
    ForceEscalate,
    RequireApproval,
)

_MAX_MONEY_DECIMAL_SCALE_EXPONENT = -2


class ObligationId(StrEnum):
    """Named obligations the policy driver may require an action to satisfy."""

    CASE_ACTIONABLE = "case_actionable"
    DENIAL_LOOP = "denial_loop"
    VERIFIED = "verified"
    NOT_ALREADY_VERIFIED = "not_already_verified"
    ORDER_OWNED = "order_owned"
    LINKED_ORDER = "linked_order"
    AMOUNT_SANE = "amount_sane"
    NO_OPEN_REFUND = "no_open_refund"
    WITHIN_REMAINDER = "within_remainder"
    THRESHOLD = "threshold"


# Applies to every action. Not per-type, therefore not omittable.
UNIVERSAL_OBLIGATIONS: Final = (
    ObligationId.CASE_ACTIONABLE,
    ObligationId.DENIAL_LOOP,
)

# Normative order, declared exactly once (ARCHITECTURE.md §10.2).
CANONICAL_ORDER: Final = (
    ObligationId.CASE_ACTIONABLE,
    ObligationId.DENIAL_LOOP,
    ObligationId.VERIFIED,
    ObligationId.NOT_ALREADY_VERIFIED,
    ObligationId.ORDER_OWNED,
    ObligationId.LINKED_ORDER,
    ObligationId.AMOUNT_SANE,
    ObligationId.NO_OPEN_REFUND,
    ObligationId.WITHIN_REMAINDER,
    ObligationId.THRESHOLD,
)


@dataclass(frozen=True, slots=True)
class Obligations:
    """A signed decision about what one action type must satisfy."""

    required: frozenset[ObligationId]
    rationale: str


ACTION_OBLIGATIONS: dict[type[_ActionBase], Obligations] = {
    GetOrders: Obligations(
        frozenset({ObligationId.VERIFIED}),
        "reads customer order identifiers",
    ),
    LinkOrder: Obligations(
        frozenset({ObligationId.VERIFIED, ObligationId.ORDER_OWNED}),
        "reads order data and writes case linkage; the order id is an untrusted claim",
    ),
    ProposeRefund: Obligations(
        frozenset(
            {
                ObligationId.VERIFIED,
                ObligationId.LINKED_ORDER,
                ObligationId.AMOUNT_SANE,
                ObligationId.NO_OPEN_REFUND,
                ObligationId.WITHIN_REMAINDER,
                ObligationId.THRESHOLD,
            }
        ),
        "the only action that moves money",
    ),
    RequestVerification: Obligations(
        frozenset({ObligationId.NOT_ALREADY_VERIFIED}),
        "external effect, but the whole point is that it runs while unverified",
    ),
    SendReply: Obligations(
        frozenset(),
        "content is ungoverned by design (see DEBT.md); the gate decides only whether "
        "a message may go out, and the recipient is derived from the case",
    ),
    Escalate: Obligations(
        frozenset(),
        "hands the case to a human and closes it; nothing to restrict beyond the "
        "universal checks. Note DEBT.md: policy cannot yet express a per-customer "
        "or time-window limit on ticket creation",
    ),
    Finish: Obligations(
        frozenset(),
        "terminal, no external effect, summary is untrusted text never read by a rule",
    ),
}


Check = Callable[[RuleContext, Action], CheckResult]


def _miswired_obligation(obligation_id: ObligationId, action: Action) -> TypeError:
    return TypeError(
        f"{obligation_id.value} is miswired for action type {type(action).__name__}"
    )


def deny_if_case_not_actionable(ctx: RuleContext) -> CheckResult:
    """Refuse actions while the case is suspended or closed."""
    if ctx.case.status is not CaseStatus.OPEN:
        return Deny(
            rule="R_CASE_NOT_ACTIONABLE",
            agent_reason=(
                "This case is not open for agent actions. Wait for operator "
                "approval, customer verification, or case closure to resolve."
            ),
            customer_reason=(
                "We cannot process this request while your case is waiting "
                "on another step."
            ),
        )
    return CONTINUE


def escalate_if_denial_loop(ctx: RuleContext) -> CheckResult:
    """Escalate when repeated denials indicate the agent is stuck."""
    if ctx.case.consecutive_denials >= ctx.denial_loop_threshold:
        return ForceEscalate(
            rule="R_DENIAL_LOOP",
            reason=(
                "The agent received too many consecutive denials and must "
                "escalate to a human operator."
            ),
        )
    return CONTINUE


def deny_if_unverified(ctx: RuleContext) -> CheckResult:
    """Refuse actions that require verification until the customer is verified."""
    if ctx.customer.verified:
        return CONTINUE
    return Deny(
        rule="R_VERIFIED",
        agent_reason=(
            "The customer is not verified. Use request_verification to unlock "
            "order data and refunds. send_reply, escalate, and finish remain "
            "available."
        ),
        customer_reason=(
            "Please verify your identity before we can access order details "
            "or process refunds."
        ),
    )


def deny_if_already_verified(ctx: RuleContext) -> CheckResult:
    """Refuse duplicate verification requests."""
    if not ctx.customer.verified:
        return CONTINUE
    return Deny(
        rule="R_ALREADY_VERIFIED",
        agent_reason="The customer is already verified.",
        customer_reason="Your identity has already been verified.",
    )


def deny_if_order_not_owned(ctx: RuleContext, action: LinkOrder) -> CheckResult:
    """Refuse linking an order that does not belong to the customer."""
    if action.order_id in ctx.customer_order_ids:
        return CONTINUE
    return Deny(
        rule="R_ORDER_OWNERSHIP",
        agent_reason=(f"Order {action.order_id} does not belong to this customer."),
        customer_reason=("We could not find that order on your account."),
    )


def deny_if_no_linked_order(ctx: RuleContext) -> CheckResult:
    """Refuse money movement before the case establishes an order."""
    if ctx.case.linked_order_id is not None:
        return CONTINUE
    return Deny(
        rule="R_NO_LINKED_ORDER",
        agent_reason=("Link an order to this case before proposing a refund."),
        customer_reason=("We need to identify the order before processing a refund."),
    )


def deny_if_amount_unsound(action: ProposeRefund) -> CheckResult:
    """Reject non-finite, non-positive, or over-scaled refund amounts."""
    amount = action.amount
    if not amount.is_finite():
        return Deny(
            rule="R_AMOUNT_SANE",
            agent_reason="Refund amount must be a finite number.",
            customer_reason="The refund amount is not valid.",
        )
    if amount <= 0:
        return Deny(
            rule="R_AMOUNT_SANE",
            agent_reason="Refund amount must be greater than zero.",
            customer_reason="The refund amount must be greater than zero.",
        )
    scale_exponent = amount.as_tuple().exponent
    if (
        isinstance(scale_exponent, int)
        and scale_exponent < _MAX_MONEY_DECIMAL_SCALE_EXPONENT
    ):
        return Deny(
            rule="R_AMOUNT_SANE",
            agent_reason="Refund amount may have at most two decimal places.",
            customer_reason="The refund amount is not valid.",
        )
    return CONTINUE


def deny_if_open_refund(ctx: RuleContext) -> CheckResult:
    """Refuse a new proposal while another refund is still open."""
    linked_order = ctx.linked_order
    if linked_order is None or not linked_order.has_open_refund:
        return CONTINUE
    return Deny(
        rule="R_OPEN_REFUND",
        agent_reason=("This order already has an open refund request."),
        customer_reason=("There is already a refund in progress for this order."),
    )


def deny_if_exceeds_remainder(
    ctx: RuleContext,
    action: ProposeRefund,
) -> CheckResult:
    """Refuse refund amounts above the order's refundable remainder."""
    linked_order = ctx.linked_order
    if linked_order is None:
        return CONTINUE
    if action.amount <= linked_order.refundable_remainder:
        return CONTINUE
    return Deny(
        rule="R_REMAINDER",
        agent_reason=(
            f"Refund amount {action.amount} exceeds the refundable remainder "
            f"{linked_order.refundable_remainder}."
        ),
        customer_reason=(
            "The requested refund is larger than the remaining refundable "
            "amount for this order."
        ),
    )


def approval_if_over_threshold(
    ctx: RuleContext,
    action: ProposeRefund,
) -> CheckResult:
    """Route large cumulative refunds to operator approval."""
    linked_order = ctx.linked_order
    if linked_order is None:
        return CONTINUE
    cumulative_total = linked_order.refunded_total + action.amount
    if cumulative_total <= ctx.refund_approval_threshold:
        return CONTINUE
    return RequireApproval(
        rule="R_THRESHOLD",
        reason=(
            f"Cumulative refunds of {cumulative_total} exceed the automatic "
            f"approval threshold of {ctx.refund_approval_threshold}."
        ),
    )


def _check_case_actionable(ctx: RuleContext, action: Action) -> CheckResult:
    del action
    return deny_if_case_not_actionable(ctx)


def _check_denial_loop(ctx: RuleContext, action: Action) -> CheckResult:
    del action
    return escalate_if_denial_loop(ctx)


def _check_verified(ctx: RuleContext, action: Action) -> CheckResult:
    del action
    return deny_if_unverified(ctx)


def _check_not_already_verified(ctx: RuleContext, action: Action) -> CheckResult:
    if not isinstance(action, RequestVerification):
        raise _miswired_obligation(ObligationId.NOT_ALREADY_VERIFIED, action)
    del action
    return deny_if_already_verified(ctx)


def _check_order_owned(ctx: RuleContext, action: Action) -> CheckResult:
    if not isinstance(action, LinkOrder):
        raise _miswired_obligation(ObligationId.ORDER_OWNED, action)
    return deny_if_order_not_owned(ctx, action)


def _check_linked_order(ctx: RuleContext, action: Action) -> CheckResult:
    if not isinstance(action, ProposeRefund):
        raise _miswired_obligation(ObligationId.LINKED_ORDER, action)
    del action
    return deny_if_no_linked_order(ctx)


def _check_amount_sane(ctx: RuleContext, action: Action) -> CheckResult:
    del ctx
    if not isinstance(action, ProposeRefund):
        raise _miswired_obligation(ObligationId.AMOUNT_SANE, action)
    return deny_if_amount_unsound(action)


def _check_no_open_refund(ctx: RuleContext, action: Action) -> CheckResult:
    if not isinstance(action, ProposeRefund):
        raise _miswired_obligation(ObligationId.NO_OPEN_REFUND, action)
    del action
    return deny_if_open_refund(ctx)


def _check_within_remainder(ctx: RuleContext, action: Action) -> CheckResult:
    if not isinstance(action, ProposeRefund):
        raise _miswired_obligation(ObligationId.WITHIN_REMAINDER, action)
    return deny_if_exceeds_remainder(ctx, action)


def _check_threshold(ctx: RuleContext, action: Action) -> CheckResult:
    if not isinstance(action, ProposeRefund):
        raise _miswired_obligation(ObligationId.THRESHOLD, action)
    return approval_if_over_threshold(ctx, action)


CHECKS: dict[ObligationId, Check] = {
    ObligationId.CASE_ACTIONABLE: _check_case_actionable,
    ObligationId.DENIAL_LOOP: _check_denial_loop,
    ObligationId.VERIFIED: _check_verified,
    ObligationId.NOT_ALREADY_VERIFIED: _check_not_already_verified,
    ObligationId.ORDER_OWNED: _check_order_owned,
    ObligationId.LINKED_ORDER: _check_linked_order,
    ObligationId.AMOUNT_SANE: _check_amount_sane,
    ObligationId.NO_OPEN_REFUND: _check_no_open_refund,
    ObligationId.WITHIN_REMAINDER: _check_within_remainder,
    ObligationId.THRESHOLD: _check_threshold,
}


def applicable_obligations(action: Action) -> frozenset[ObligationId]:
    """Return every obligation the driver will evaluate for one action type."""
    obligations = ACTION_OBLIGATIONS.get(type(action))
    if obligations is None:
        return frozenset()
    return frozenset(UNIVERSAL_OBLIGATIONS) | obligations.required


__all__ = [
    "ACTION_OBLIGATIONS",
    "CANONICAL_ORDER",
    "CHECKS",
    "UNIVERSAL_OBLIGATIONS",
    "Check",
    "ObligationId",
    "Obligations",
    "applicable_obligations",
    "approval_if_over_threshold",
    "deny_if_already_verified",
    "deny_if_amount_unsound",
    "deny_if_case_not_actionable",
    "deny_if_exceeds_remainder",
    "deny_if_no_linked_order",
    "deny_if_open_refund",
    "deny_if_order_not_owned",
    "deny_if_unverified",
    "escalate_if_denial_loop",
]

"""Ordered policy generator and fail-closed evaluator."""

from saferefund.actions.models import Action
from saferefund.policy.authorisation import Authorisation
from saferefund.policy.checks import (
    ACTION_OBLIGATIONS,
    CANONICAL_ORDER,
    CHECKS,
    UNIVERSAL_OBLIGATIONS,
    applicable_obligations,
)
from saferefund.policy.context import RuleContext
from saferefund.policy.verdicts import (
    CONTINUE,
    Allow,
    Deny,
    ForceEscalate,
    RequireApproval,
    Verdict,
)


def evaluate(ctx: RuleContext, action: Action) -> Verdict:
    """Return the first decisive verdict, or fail closed when none is produced."""
    obligations = ACTION_OBLIGATIONS.get(type(action))
    if obligations is None:
        return Deny(
            rule="R_EXHAUSTED",
            agent_reason="No rule produced a verdict.",
            customer_reason="We cannot process this request automatically.",
        )

    applicable = frozenset(UNIVERSAL_OBLIGATIONS) | obligations.required
    for obligation_id in CANONICAL_ORDER:
        if obligation_id in applicable:
            result = CHECKS[obligation_id](ctx, action)
            if result is not CONTINUE:
                return result
    return Allow()


def authorise(
    ctx: RuleContext,
    action: Action,
) -> Authorisation | Deny | RequireApproval | ForceEscalate:
    """Return a single-use capability for an allowed proposal, or a blocking verdict."""
    verdict = evaluate(ctx, action)
    if isinstance(verdict, Allow):
        return Authorisation(
            case_id=ctx.case.case_id,
            action=action,
            obligations_discharged=applicable_obligations(action),
        )
    return verdict


__all__ = [
    "applicable_obligations",
    "authorise",
    "evaluate",
]

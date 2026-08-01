"""Characterization tests for the obligation-coverage policy migration.

The reference evaluator below encodes pre-migration semantics independently of
``ACTION_OBLIGATIONS`` and ``CHECKS``. It uses label-guarded applicability and
``EXPLICITLY_PERMITTED_ACTIONS``-style terminal permission, which the new driver
replaces with signed coverage and driver-only ``Allow``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, assert_never, cast

import pytest

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
from saferefund.config import DENIAL_LOOP_THRESHOLD, REFUND_APPROVAL_THRESHOLD
from saferefund.domain.enums import CaseStatus
from saferefund.policy.checks import (
    approval_if_over_threshold,
    deny_if_already_verified,
    deny_if_amount_unsound,
    deny_if_case_not_actionable,
    deny_if_exceeds_remainder,
    deny_if_no_linked_order,
    deny_if_open_refund,
    deny_if_order_not_owned,
    deny_if_unverified,
    escalate_if_denial_loop,
)
from saferefund.policy.policy import evaluate
from saferefund.policy.verdicts import (
    CONTINUE,
    Allow,
    CheckResult,
    Deny,
    ForceEscalate,
    RequireApproval,
    Verdict,
)
from saferefund.repositories.seed import ORD_1001_ID, ORD_1003_ID, ORD_2001_ID
from tests.unit.labels_helpers import Label, has_label
from tests.unit.policy_helpers import (
    open_case_summary,
    order_summary,
    rule_context,
    tom_unverified_context,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from saferefund.policy.context import RuleContext

_LEGACY_EXPLICITLY_PERMITTED: frozenset[type[_ActionBase]] = frozenset(
    (
        GetOrders,
        LinkOrder,
        ProposeRefund,
        SendReply,
        RequestVerification,
        Escalate,
        Finish,
    )
)


def _legacy_deny_if_unverified(ctx: RuleContext, action: Action) -> CheckResult:
    if ctx.customer.verified:
        return CONTINUE
    if not has_label(action, Label.READS_PII) and not has_label(
        action, Label.MOVES_MONEY
    ):
        return CONTINUE
    return deny_if_unverified(ctx)


def _legacy_deny_if_already_verified(
    ctx: RuleContext,
    action: Action,
) -> CheckResult:
    if not isinstance(action, RequestVerification):
        return CONTINUE
    return deny_if_already_verified(ctx)


def _legacy_perform_checks(
    ctx: RuleContext,
    action: Action,
) -> Iterator[CheckResult | Allow]:
    yield deny_if_case_not_actionable(ctx)
    yield escalate_if_denial_loop(ctx)
    yield _legacy_deny_if_unverified(ctx, action)
    yield _legacy_deny_if_already_verified(ctx, action)

    if isinstance(action, LinkOrder):
        yield deny_if_order_not_owned(ctx, action)

    if has_label(action, Label.MOVES_MONEY):
        refund_action = cast("ProposeRefund", action)
        yield deny_if_no_linked_order(ctx)
        yield deny_if_amount_unsound(refund_action)
        yield deny_if_open_refund(ctx)
        yield deny_if_exceeds_remainder(ctx, refund_action)

    if has_label(action, Label.MOVES_MONEY):
        yield approval_if_over_threshold(ctx, cast("ProposeRefund", action))

    if type(action) in _LEGACY_EXPLICITLY_PERMITTED:
        yield Allow()


def _legacy_evaluate(ctx: RuleContext, action: Action) -> Verdict:
    for result in _legacy_perform_checks(ctx, action):
        if result is not CONTINUE:
            return result
    return Deny(
        rule="R_EXHAUSTED",
        agent_reason="No rule produced a verdict.",
        customer_reason="We cannot process this request automatically.",
    )


def _verdict_key(verdict: Verdict) -> tuple[str, ...]:
    if isinstance(verdict, Allow):
        return ("allow",)
    if isinstance(verdict, Deny):
        return ("deny", verdict.rule)
    if isinstance(verdict, RequireApproval):
        return ("approval", verdict.rule)
    if isinstance(verdict, ForceEscalate):
        return ("escalate", verdict.rule)
    assert_never(verdict)


@dataclass(frozen=True, slots=True)
class _Scenario:
    id: str
    ctx: RuleContext
    action: Action


def _all_actions() -> tuple[Action, ...]:
    return (
        GetOrders(action="get_orders"),
        LinkOrder(action="link_order", order_id=ORD_1001_ID),
        ProposeRefund(action="propose_refund", amount=Decimal("10.00")),
        SendReply(action="send_reply", subject="Hi", body="There"),
        RequestVerification(action="request_verification"),
        Escalate(action="escalate", reason="needs human"),
        Finish(action="finish", summary="resolved"),
    )


def _scenario_matrix() -> list[_Scenario]:
    scenarios: list[_Scenario] = []

    base_contexts: list[tuple[str, RuleContext]] = [
        ("verified-open", rule_context()),
        ("unverified-open", tom_unverified_context()),
        (
            "closed-case",
            rule_context(case=open_case_summary(status=CaseStatus.CLOSED)),
        ),
        (
            "awaiting-approval",
            rule_context(
                case=open_case_summary(status=CaseStatus.AWAITING_APPROVAL),
            ),
        ),
        (
            "awaiting-verification",
            rule_context(
                case=open_case_summary(status=CaseStatus.AWAITING_VERIFICATION),
            ),
        ),
        (
            "denial-loop",
            rule_context(
                case=open_case_summary(consecutive_denials=DENIAL_LOOP_THRESHOLD),
            ),
        ),
        (
            "linked-order-open-refund",
            rule_context(
                case=open_case_summary(linked_order_id=ORD_1003_ID),
                linked_order=order_summary(
                    order_id=ORD_1003_ID,
                    total=Decimal("780.00"),
                    refunded_total=Decimal("300.00"),
                    has_open_refund=True,
                ),
            ),
        ),
        (
            "linked-order-over-remainder",
            rule_context(
                case=open_case_summary(linked_order_id=ORD_1003_ID),
                linked_order=order_summary(
                    order_id=ORD_1003_ID,
                    total=Decimal("780.00"),
                    refunded_total=Decimal("300.00"),
                ),
            ),
        ),
        (
            "linked-order-over-threshold",
            rule_context(
                case=open_case_summary(linked_order_id=ORD_2001_ID),
                linked_order=order_summary(
                    order_id=ORD_2001_ID,
                    total=Decimal("800.00"),
                    refunded_total=Decimal("300.00"),
                ),
                customer_order_ids=frozenset({ORD_2001_ID}),
            ),
        ),
        (
            "unverified-linked-refund",
            tom_unverified_context(
                linked_order=order_summary(
                    order_id=ORD_2001_ID,
                    total=Decimal("800.00"),
                ),
            ),
        ),
        (
            "foreign-order-link",
            rule_context(customer_order_ids=frozenset({ORD_1001_ID})),
        ),
        (
            "no-linked-order-refund",
            rule_context(case=open_case_summary(linked_order_id=None)),
        ),
    ]

    refund_amounts = {
        ProposeRefund: Decimal("10.00"),
    }
    link_orders = {
        LinkOrder: ORD_1001_ID,
    }

    for ctx_name, ctx in base_contexts:
        for action in _all_actions():
            concrete = action
            if isinstance(action, ProposeRefund):
                if ctx_name == "linked-order-over-remainder":
                    amount = Decimal("900.00")
                elif ctx_name == "linked-order-over-threshold":
                    amount = Decimal("201.00")
                elif ctx_name == "unverified-linked-refund":
                    amount = Decimal("800.00")
                else:
                    amount = refund_amounts[ProposeRefund]
                concrete = ProposeRefund(action="propose_refund", amount=amount)
            elif isinstance(action, LinkOrder):
                order_id = (
                    ORD_2001_ID
                    if ctx_name == "foreign-order-link"
                    else link_orders[LinkOrder]
                )
                concrete = LinkOrder(action="link_order", order_id=order_id)

            scenarios.append(
                _Scenario(
                    id=f"{ctx_name}:{concrete.action}",
                    ctx=ctx,
                    action=concrete,
                )
            )

    return scenarios


@pytest.mark.parametrize(
    "scenario",
    _scenario_matrix(),
    ids=lambda scenario: scenario.id,
)
def test_migration_preserves_legacy_verdicts(scenario: _Scenario) -> None:
    """Mutation: reorder ``CANONICAL_ORDER`` or drop one type-specific obligation."""
    legacy = _legacy_evaluate(scenario.ctx, scenario.action)
    current = evaluate(scenario.ctx, scenario.action)
    assert _verdict_key(current) == _verdict_key(legacy)


def test_legacy_and_current_agree_on_threshold_boundary() -> None:
    """Mutation: move ``THRESHOLD`` above ``WITHIN_REMAINDER`` in canonical order."""
    ctx = rule_context(
        case=open_case_summary(linked_order_id=ORD_1003_ID),
        linked_order=order_summary(
            order_id=ORD_1003_ID,
            total=Decimal("780.00"),
            refunded_total=Decimal("300.00"),
        ),
        refund_approval_threshold=REFUND_APPROVAL_THRESHOLD,
    )
    action = ProposeRefund(action="propose_refund", amount=Decimal("200.00"))
    assert _verdict_key(evaluate(ctx, action)) == _verdict_key(
        _legacy_evaluate(ctx, action)
    )
    assert isinstance(evaluate(ctx, action), Allow)

    over = ProposeRefund(action="propose_refund", amount=Decimal("201.00"))
    assert _verdict_key(evaluate(ctx, over)) == _verdict_key(
        _legacy_evaluate(ctx, over)
    )
    assert isinstance(evaluate(ctx, over), RequireApproval)

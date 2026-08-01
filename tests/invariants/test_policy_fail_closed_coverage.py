"""The policy driver must actually be fail-closed.

Coverage is a signed decision per action type. Absent table entries and empty
obligation sets are distinct: only the former yields ``R_EXHAUSTED``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict

from saferefund.actions.models import (
    ACTION_MODEL_CLASSES,
    Action,
    Escalate,
    SendReply,
    _ActionBase,
)
from saferefund.policy.checks import (
    ACTION_OBLIGATIONS,
    UNIVERSAL_OBLIGATIONS,
)
from saferefund.policy.policy import evaluate
from saferefund.policy.verdicts import Deny
from tests.unit.policy_helpers import rule_context

if TYPE_CHECKING:
    import pytest


class RescindRefund(BaseModel):
    """A newly added action whose author labelled it but forgot its policy rule."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["rescind_refund"] = "rescind_refund"


def _unmatched_action() -> object:
    return cast("object", RescindRefund())


def test_absent_and_empty_coverage_are_distinguishable() -> None:
    """Mutation: default ``ACTION_OBLIGATIONS.get`` to empty ``Obligations``."""
    assert ACTION_OBLIGATIONS.get(cast("type[_ActionBase]", RescindRefund)) is None
    assert ACTION_OBLIGATIONS[Escalate].required == frozenset()
    assert ACTION_OBLIGATIONS[Escalate].rationale.strip() != ""


def test_removing_a_real_action_from_the_table_denies_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: delete ``SendReply`` from ``ACTION_OBLIGATIONS``."""
    send_reply_type: type[_ActionBase] = SendReply
    monkeypatch.delitem(ACTION_OBLIGATIONS, send_reply_type)

    verdict = evaluate(
        rule_context(),
        SendReply(action="send_reply", subject="Hi", body="There"),
    )

    assert isinstance(verdict, Deny)
    assert verdict.rule == "R_EXHAUSTED"


def test_universal_obligations_apply_to_every_action_type() -> None:
    """Mutation: fold ``UNIVERSAL_OBLIGATIONS`` into one per-type ``required`` set."""
    for action_type in ACTION_OBLIGATIONS:
        assert set(UNIVERSAL_OBLIGATIONS).isdisjoint(
            ACTION_OBLIGATIONS[action_type].required
        )


def test_every_action_model_class_has_a_recorded_decision() -> None:
    """Mutation: remove one class from ``ACTION_OBLIGATIONS`` silently."""
    assert set(ACTION_MODEL_CLASSES) - set(ACTION_OBLIGATIONS) == set()


def test_exhausted_denial_is_reachable_for_uncovered_action_type() -> None:
    """Mutation: return ``Allow()`` when ``ACTION_OBLIGATIONS.get`` is ``None``."""
    verdict = evaluate(rule_context(), cast("Action", RescindRefund()))

    assert isinstance(verdict, Deny)
    assert verdict.rule == "R_EXHAUSTED"

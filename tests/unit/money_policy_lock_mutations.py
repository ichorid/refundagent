"""Synthetic sources for money-policy lock-contract mutation guards."""

from __future__ import annotations

from typing import Final

PRODUCTION_SAFE_REFUND_ROUTER = """
async def refund_first_router(session, refund_session_factory, case_id, action):
    if isinstance(action, ProposeRefund):
        case_row = await session.get(CaseRow, case_id)
        if case_row is None:
            raise CaseNotFoundError(case_id)
        verdict = await execute_propose_refund(
            session,
            refund_session_factory,
            case_row=case_row,
            case_id=case_id,
            action=action,
        )
        return await _finalize_propose_refund_verdict(session, case_id, verdict)
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
"""

NESTED_AUTHORISE_NOISE_ROUTER = """
async def outer_with_nested_noise(session, refund_session_factory, case_id, action):
    async def nested_noise():
        _, rule_context = await load_rule_context_for_case(session, case_id)
        return authorise(rule_context, action)

    if isinstance(action, ProposeRefund):
        case_row = await session.get(CaseRow, case_id)
        if case_row is None:
            raise CaseNotFoundError(case_id)
        verdict = await execute_propose_refund(
            session,
            refund_session_factory,
            case_row=case_row,
            case_id=case_id,
            action=action,
        )
        return await _finalize_propose_refund_verdict(session, case_id, verdict)
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
"""

LOCKED_EVALUATOR = """
async def locked_money_path(session, customer_id, case_id, action):
    await lock_customer_for_update(session, customer_id)
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
"""

_GOLDEN_SHAPE_B = """async def {name}(session, refund_session_factory, case_id, action):
    if isinstance(action, ProposeRefund):
        case_row = await session.get(CaseRow, case_id)
        if case_row is None:
            raise CaseNotFoundError(case_id)
        verdict = await execute_propose_refund(
            session,
            refund_session_factory,
            case_row=case_row,
            case_id=case_id,
            action=action,
        )
        return await _finalize_propose_refund_verdict(session, case_id, verdict)
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
"""

_SHAPE_B_TOKEN_MUTATIONS: Final[tuple[tuple[str, str, str], ...]] = (
    ("wrong_case_id_in_get", "case_id", "other_case_id"),
    ("wrong_action_in_delegate", "action=action", "action=other_action"),
    ("wrong_finalizer_verdict", "case_id, verdict", "case_id, other_verdict"),
    (
        "extra_delegate_keyword",
        "action=action,\n        )",
        "action=action,\n            extra=True,\n        )",
    ),
    (
        "missing_delegate_positional",
        "session,\n            refund_session_factory,",
        "session,",
    ),
    (
        "starred_delegate_args",
        "session,\n            refund_session_factory,",
        "session, *extra,",
    ),
)

_HAND_WRITTEN_REJECTED: Final[tuple[tuple[str, str], ...]] = (
    (
        "authorise_before_guard",
        """
async def authorise_before_guard(session, refund_session_factory, case_id, action):
    _, rule_context = await load_rule_context_for_case(session, case_id)
    verdict = authorise(rule_context, action)
    if isinstance(action, ProposeRefund):
        case_row = await session.get(CaseRow, case_id)
        if case_row is None:
            raise CaseNotFoundError(case_id)
        verdict = await execute_propose_refund(
            session,
            refund_session_factory,
            case_row=case_row,
            case_id=case_id,
            action=action,
        )
        return await _finalize_propose_refund_verdict(session, case_id, verdict)
    return verdict
""",
    ),
    (
        "fake_lock_method",
        """
async def fake_lock_method(session, customer_id, case_id, action, attacker):
    await attacker.lock_customer_for_update(session, customer_id)
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "wrong_factory",
        """
async def wrong_factory(session, refund_session_factory, case_id, action):
    if isinstance(action, ProposeRefund):
        case_row = await session.get(CaseRow, case_id)
        if case_row is None:
            raise CaseNotFoundError(case_id)
        verdict = await execute_propose_refund(
            session, factory, case_row=case_row, case_id=case_id, action=action,
        )
        return await _finalize_propose_refund_verdict(session, case_id, verdict)
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "fake_delegate_method",
        """
async def fake_delegate_method(
    session, refund_session_factory, case_id, action, attacker,
):
    if isinstance(action, ProposeRefund):
        case_row = await session.get(CaseRow, case_id)
        if case_row is None:
            raise CaseNotFoundError(case_id)
        verdict = await attacker.execute_propose_refund(
            session,
            refund_session_factory,
            case_row=case_row,
            case_id=case_id,
            action=action,
        )
        return await _finalize_propose_refund_verdict(session, case_id, verdict)
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "fake_finalizer_method",
        """
async def fake_finalizer_method(
    session, refund_session_factory, case_id, action, attacker,
):
    if isinstance(action, ProposeRefund):
        case_row = await session.get(CaseRow, case_id)
        if case_row is None:
            raise CaseNotFoundError(case_id)
        verdict = await execute_propose_refund(
            session,
            refund_session_factory,
            case_row=case_row,
            case_id=case_id,
            action=action,
        )
        return await attacker._finalize_propose_refund_verdict(
            session, case_id, verdict,
        )
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "preliminary_router",
        """
async def preliminary_router(session, factory, case_row, case_id, action):
    _, rule_context = await load_rule_context_for_case(session, case_id)
    verdict = authorise(rule_context, action)
    if isinstance(verdict, Deny):
        return verdict
    return await execute_propose_refund(
        session, factory, case_row=case_row, case_id=case_id, action=action,
    )
""",
    ),
    (
        "optional_refund_router",
        """
async def optional_refund_router(
    session, factory, case_row, case_id, action, use_locked_path,
):
    if use_locked_path:
        return await execute_propose_refund(
            session, factory, case_row=case_row, case_id=case_id, action=action,
        )
    _, rule_context = await load_rule_context_for_case(session, case_id)
    verdict = authorise(rule_context, action)
    if isinstance(verdict, Deny):
        return verdict
    return verdict
""",
    ),
    (
        "unrelated_delegate_router",
        """
async def unrelated_delegate_router(session, factory, case_row, case_id, action):
    await execute_propose_refund(
        session, factory, case_row=case_row, case_id=case_id, action=action,
    )
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "inverted_guard_router",
        """
async def inverted_guard_router(session, case_id, action):
    if not isinstance(action, ProposeRefund):
        _, rule_context = await load_rule_context_for_case(session, case_id)
        return authorise(rule_context, action)
    return await execute_propose_refund(
        session, None, case_row=None, case_id=case_id, action=action,
    )
""",
    ),
    (
        "fallthrough_refund_router",
        """
async def fallthrough_refund_router(session, factory, case_row, case_id, action):
    if isinstance(action, ProposeRefund):
        await execute_propose_refund(
            session, factory, case_row=case_row, case_id=case_id, action=action,
        )
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "conditional_refund_delegate",
        """
async def conditional_refund_delegate(session, factory, case_row, case_id, action):
    if isinstance(action, ProposeRefund):
        if action.amount > 100:
            return await execute_propose_refund(
                session, factory, case_row=case_row, case_id=case_id, action=action,
            )
        return Deny(reason="too small")
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "conditional_customer_lock",
        """
async def conditional_customer_lock(session, customer_id, case_id, action, use_lock):
    if use_lock:
        await lock_customer_for_update(session, customer_id)
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "else_refund_router",
        """
async def else_refund_router(session, factory, case_id, action):
    if isinstance(action, ProposeRefund):
        case_row = await session.get(CaseRow, case_id)
        if case_row is None:
            raise CaseNotFoundError(case_id)
        verdict = await execute_propose_refund(
            session, factory, case_row=case_row, case_id=case_id, action=action,
        )
        return await _finalize_propose_refund_verdict(session, case_id, verdict)
    else:
        return await execute_propose_refund(
            session, factory, case_row=None, case_id=case_id, action=action,
        )
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "elif_refund_router",
        """
async def elif_refund_router(session, factory, case_id, action):
    if isinstance(action, ProposeRefund):
        case_row = await session.get(CaseRow, case_id)
        if case_row is None:
            raise CaseNotFoundError(case_id)
        verdict = await execute_propose_refund(
            session, factory, case_row=case_row, case_id=case_id, action=action,
        )
        return await _finalize_propose_refund_verdict(session, case_id, verdict)
    elif isinstance(action, ProposeRefund):
        return await execute_propose_refund(
            session, factory, case_row=None, case_id=case_id, action=action,
        )
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "nested_try_refund_router",
        """
async def nested_try_refund_router(session, factory, case_id, action):
    if isinstance(action, ProposeRefund):
        try:
            case_row = await session.get(CaseRow, case_id)
            if case_row is None:
                raise CaseNotFoundError(case_id)
            verdict = await execute_propose_refund(
                session, factory, case_row=case_row, case_id=case_id, action=action,
            )
            return await _finalize_propose_refund_verdict(session, case_id, verdict)
        except CaseNotFoundError:
            raise
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "delayed_lock_path",
        """
async def delayed_lock_path(session, customer_id, case_id, action):
    _ = customer_id
    await lock_customer_for_update(session, customer_id)
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "nested_helper_lock_path",
        """
async def nested_helper_lock_path(session, customer_id, case_id, action):
    async def acquire():
        await lock_customer_for_update(session, customer_id)
    await acquire()
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "second_refund_guard_before_safe",
        """
async def second_refund_guard_before_safe(
    session, refund_session_factory, case_id, action,
):
    if isinstance(action, ProposeRefund):
        return await execute_propose_refund(
            session,
            refund_session_factory,
            case_row=None,
            case_id=case_id,
            action=action,
        )
    if isinstance(action, ProposeRefund):
        case_row = await session.get(CaseRow, case_id)
        if case_row is None:
            raise CaseNotFoundError(case_id)
        verdict = await execute_propose_refund(
            session,
            refund_session_factory,
            case_row=case_row,
            case_id=case_id,
            action=action,
        )
        return await _finalize_propose_refund_verdict(session, case_id, verdict)
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "extra_guard_body_statement",
        """
async def extra_guard_body_statement(session, refund_session_factory, case_id, action):
    if isinstance(action, ProposeRefund):
        case_row = await session.get(CaseRow, case_id)
        if case_row is None:
            raise CaseNotFoundError(case_id)
        _ = case_row.customer_id
        verdict = await execute_propose_refund(
            session,
            refund_session_factory,
            case_row=case_row,
            case_id=case_id,
            action=action,
        )
        return await _finalize_propose_refund_verdict(session, case_id, verdict)
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "wrong_session_in_lock",
        """
async def wrong_session_in_lock(session, customer_id, case_id, action):
    await lock_customer_for_update(other_session, customer_id)
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "nested_loop_refund_router",
        """
async def nested_loop_refund_router(session, factory, case_id, action):
    if isinstance(action, ProposeRefund):
        for _ in range(1):
            case_row = await session.get(CaseRow, case_id)
            if case_row is None:
                raise CaseNotFoundError(case_id)
            verdict = await execute_propose_refund(
                session, factory, case_row=case_row, case_id=case_id, action=action,
            )
            return await _finalize_propose_refund_verdict(session, case_id, verdict)
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "nested_match_refund_router",
        """
async def nested_match_refund_router(session, factory, case_id, action):
    if isinstance(action, ProposeRefund):
        match action:
            case _:
                case_row = await session.get(CaseRow, case_id)
                if case_row is None:
                    raise CaseNotFoundError(case_id)
                verdict = await execute_propose_refund(
                    session, factory, case_row=case_row, case_id=case_id, action=action,
                )
                return await _finalize_propose_refund_verdict(session, case_id, verdict)
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "prelude_keyword_bypass",
        """
async def prelude_keyword_bypass(session, session_factory, case_id, action):
    refund_session_factory = session_factory or session_factory_for(
        session=session, authorise=authorise(rule_context, action),
    )
    if isinstance(action, ProposeRefund):
        case_row = await session.get(CaseRow, case_id)
        if case_row is None:
            raise CaseNotFoundError(case_id)
        verdict = await execute_propose_refund(
            session,
            refund_session_factory,
            case_row=case_row,
            case_id=case_id,
            action=action,
        )
        return await _finalize_propose_refund_verdict(session, case_id, verdict)
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "session_get_keyword_bypass",
        """
async def session_get_keyword_bypass(session, refund_session_factory, case_id, action):
    if isinstance(action, ProposeRefund):
        case_row = await session.get(CaseRow, case_id=case_id)
        if case_row is None:
            raise CaseNotFoundError(case_id)
        verdict = await execute_propose_refund(
            session,
            refund_session_factory,
            case_row=case_row,
            case_id=case_id,
            action=action,
        )
        return await _finalize_propose_refund_verdict(session, case_id, verdict)
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "isinstance_keyword_bypass",
        """
async def isinstance_keyword_bypass(session, refund_session_factory, case_id, action):
    if isinstance(action, ProposeRefund, extra=True):
        case_row = await session.get(CaseRow, case_id)
        if case_row is None:
            raise CaseNotFoundError(case_id)
        verdict = await execute_propose_refund(
            session,
            refund_session_factory,
            case_row=case_row,
            case_id=case_id,
            action=action,
        )
        return await _finalize_propose_refund_verdict(session, case_id, verdict)
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "gate_wrapper_import",
        """
from saferefund.gate.operations import authorise as gate_authorise

async def gate_wrapper_import(session, case_id, action):
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return gate_authorise(rule_context, action)
""",
    ),
    (
        "gate_operations_dotted_import",
        """
import saferefund.gate.operations

async def gate_operations_dotted_import(session, case_id, action):
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return saferefund.gate.operations.authorise(rule_context, action)
""",
    ),
    (
        "gate_operations_package_parent",
        """
from saferefund.gate import operations

async def gate_operations_package_parent(session, case_id, action):
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return operations.authorise(rule_context, action)
""",
    ),
    (
        "from_package_module_alias",
        """
from saferefund.policy import policy as p

async def from_package_module_alias(session, case_id, action):
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return p.authorise(rule_context, action)
""",
    ),
    (
        "colliding_nested",
        """
async def shared(session, customer_id, case_id, action):
    await lock_customer_for_update(session, customer_id)
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)

async def outer_router(session, case_id, action):
    async def shared():
        _, rule_context = await load_rule_context_for_case(session, case_id)
        return authorise(rule_context, action)
    return shared
""",
    ),
    (
        "deep_nested_trusted_call",
        """
async def deep_nested_owner(session, case_id, action):
  if case_id:
    for _ in range(1):
      try:
        match action:
          case _:
            async def level3():
              _, rule_context = await load_rule_context_for_case(session, case_id)
              return authorise(rule_context, action)
            return level3
      except RuntimeError:
        pass
  _, rule_context = await load_rule_context_for_case(session, case_id)
  return authorise(rule_context, action)
""",
    ),
    (
        "sibling_nested_same_name",
        """
async def branch_router(session, case_id, action, branch):
    if branch:
        async def helper():
            _, rule_context = await load_rule_context_for_case(session, case_id)
            return authorise(rule_context, action)
        return helper
    async def helper():
        _, rule_context = await load_rule_context_for_case(session, case_id)
        return authorise(rule_context, action)
    return helper
""",
    ),
    (
        "sync_top_level_owner",
        """
def sync_owner(session, customer_id, case_id, action):
    lock_customer_for_update(session, customer_id)
    _, rule_context = load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "module_level_trusted_call",
        """
_, rule_context = load_rule_context_for_case(session, case_id)
verdict = authorise(rule_context, action)
""",
    ),
    (
        "comprehension_trusted_call",
        """
async def comprehension_owner(session, customer_id, case_id, action):
    await lock_customer_for_update(session, customer_id)
    _ = [authorise(rule_context, action) for _ in [1]]
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "import_shadowed_by_local_def",
        """
from saferefund.gate.operations import authorise

def authorise(ctx, action):
    return Deny(reason="shadow")

async def import_shadowed_by_local_def(session, case_id, action):
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "guard_under_unrelated_conditional",
        """
async def guard_under_unrelated_conditional(
    session, refund_session_factory, case_id, action, debug,
):
    if debug:
        if isinstance(action, ProposeRefund):
            case_row = await session.get(CaseRow, case_id)
            if case_row is None:
                raise CaseNotFoundError(case_id)
            verdict = await execute_propose_refund(
                session,
                refund_session_factory,
                case_row=case_row,
                case_id=case_id,
                action=action,
            )
            return await _finalize_propose_refund_verdict(session, case_id, verdict)
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
    (
        "mixed_shapes_and_namesake",
        """
async def locked_money_path(session, customer_id, case_id, action):
    await lock_customer_for_update(session, customer_id)
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)

async def refund_first_router(session, refund_session_factory, case_id, action):
    if isinstance(action, ProposeRefund):
        case_row = await session.get(CaseRow, case_id)
        if case_row is None:
            raise CaseNotFoundError(case_id)
        verdict = await execute_propose_refund(
            session,
            refund_session_factory,
            case_row=case_row,
            case_id=case_id,
            action=action,
        )
        return await _finalize_propose_refund_verdict(session, case_id, verdict)
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)

async def outer_with_namesake(session, case_id, action):
    async def locked_money_path():
        _, rule_context = await load_rule_context_for_case(session, case_id)
        return authorise(rule_context, action)
    return locked_money_path
""",
    ),
    (
        "getattr_dynamic_access",
        """
from saferefund.policy import policy as policy_module

async def getattr_dynamic_access(session, case_id, action):
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return getattr(policy_module, "authorise")(rule_context, action)
""",
    ),
    (
        "rebound_trusted_import",
        """
from saferefund.policy.policy import authorise

authorise = untrusted_authorise

async def rebound_trusted_import(session, case_id, action):
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
""",
    ),
)


def _generated_shape_b_mutations() -> tuple[tuple[str, str], ...]:
    generated: list[tuple[str, str]] = []
    for name, old, new in _SHAPE_B_TOKEN_MUTATIONS:
        lines = _GOLDEN_SHAPE_B.format(name=name).split("\n", 1)
        body = lines[1].replace(old, new, 1) if len(lines) > 1 else ""
        source = lines[0] + ("\n" + body if body else "")
        generated.append((name, source))
    return tuple(generated)


REJECTED_MUTATIONS: Final[tuple[tuple[str, str], ...]] = (
    *_HAND_WRITTEN_REJECTED,
    *_generated_shape_b_mutations(),
)

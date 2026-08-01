"""Structural evidence that money-policy evaluation locks before decisive history.

This is structure evidence only; PostgreSQL concurrency tests prove contention.

What this module establishes (syntactic, fail-closed):

- Every statically identifiable trusted policy ``authorise`` call in gate modules is
  discovered via enumerated import bindings for
  ``saferefund.policy.policy.authorise`` and the verified gate wrapper
  ``saferefund.gate.operations.authorise``.
- Each such call is attributed to its true lexical owner via AST parent-chain
  resolution (collision-free qualified nesting path plus source location), never
  bare-name lookup.
- Each owner is classified in one aggregation pass: only direct module-child
  ``async def`` owners matching Shape A (direct-first customer lock) or Shape B
  (production refund-first guard skeleton) are accepted; nested, sync, and
  module-level owners are rejected.
- ``execute_propose_refund`` delegates to a Shape-A ``_persist_refund_proposal``.

What it does not establish:

- Runtime control flow, exception paths, or semantic equivalence beyond matched
  skeletons.
- Protection against evaluators that satisfy a skeleton but violate invariants
  elsewhere.
- Dynamic or reflective access (``getattr``, ``importlib``, rebound imports) to
  the policy primitive; such patterns are rejected or remain outside this claim.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest

from tests.unit.money_policy_lock_mutations import (
    LOCKED_EVALUATOR,
    NESTED_AUTHORISE_NOISE_ROUTER,
    PRODUCTION_SAFE_REFUND_ROUTER,
    REJECTED_MUTATIONS,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
GATE_DIRECTORY = SOURCE_ROOT / "saferefund" / "gate"
STRUCTURE_EVIDENCE_LABEL: Final[str] = "structure evidence"
LOCKED_REEVALUATION_ENTRY = "execute_propose_refund"
LOCKED_DECISIVE_EVALUATOR = "_persist_refund_proposal"
POLICY_MODULE: Final[str] = "saferefund.policy.policy"
POLICY_PACKAGE: Final[str] = "saferefund.policy"
GATE_PACKAGE: Final[str] = "saferefund.gate"
GATE_OPERATIONS_MODULE: Final[str] = "saferefund.gate.operations"
TRUSTED_MODULE_CHAINS: Final[frozenset[str]] = frozenset(
    {POLICY_MODULE, GATE_OPERATIONS_MODULE},
)
AUTHORISE_FROM_MODULES: Final[frozenset[str]] = frozenset(
    {POLICY_MODULE, GATE_OPERATIONS_MODULE},
)
PACKAGE_MODULE_BINDINGS: Final[dict[str, str]] = {
    POLICY_PACKAGE: "policy",
    GATE_PACKAGE: "operations",
}
SYNTHETIC_MODULE_PREFIX: Final[str] = "synthetic/"


@dataclass(frozen=True, slots=True)
class OwnerKey:
    """Collision-free lexical owner identity within one parsed module tree."""

    module_path: str
    qualified_path: tuple[str, ...]
    lineno: int

    def label(self) -> str:
        path = ".".join(self.qualified_path) if self.qualified_path else "<module>"
        return f"{self.module_path}:{path}:{self.lineno}"


@dataclass(frozen=True, slots=True)
class TrustedBindings:
    bare_names: frozenset[str]
    module_aliases: frozenset[str]


def _build_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _qualified_owner_path(
    owner: ast.AsyncFunctionDef | ast.FunctionDef,
    parents: dict[ast.AST, ast.AST],
    module_tree: ast.Module,
) -> tuple[str, ...]:
    path: list[str] = [owner.name]
    current: ast.AST = owner
    while True:
        parent = parents.get(current)
        if parent is None or parent is module_tree:
            break
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            path.insert(0, parent.name)
            current = parent
        else:
            break
    return tuple(path)


def _owner_key(
    owner: ast.AsyncFunctionDef | ast.FunctionDef | None,
    *,
    module_path: str,
    parents: dict[ast.AST, ast.AST],
    module_tree: ast.Module,
    lineno: int,
) -> OwnerKey:
    if owner is None:
        return OwnerKey(module_path, (), lineno)
    return OwnerKey(
        module_path,
        _qualified_owner_path(owner, parents, module_tree),
        owner.lineno,
    )


def _is_name(node: ast.expr, *, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _is_direct_name_call(call: ast.Call, name: str) -> bool:
    return isinstance(call.func, ast.Name) and call.func.id == name


def _collect_authorise_from_import(node: ast.ImportFrom, bare_names: set[str]) -> None:
    if node.module not in AUTHORISE_FROM_MODULES:
        return
    for alias in node.names:
        if alias.name == "authorise":
            bare_names.add(alias.asname or alias.name)


def _collect_package_module_alias(
    node: ast.ImportFrom,
    module_aliases: set[str],
) -> None:
    if node.module not in PACKAGE_MODULE_BINDINGS:
        return
    bound_name = PACKAGE_MODULE_BINDINGS[node.module]
    for alias in node.names:
        if alias.name == bound_name:
            module_aliases.add(alias.asname or alias.name)


def _collect_import_bindings(module_tree: ast.Module) -> tuple[set[str], set[str]]:
    bare_names: set[str] = set()
    module_aliases: set[str] = set()
    for node in module_tree.body:
        if isinstance(node, ast.ImportFrom):
            _collect_authorise_from_import(node, bare_names)
            _collect_package_module_alias(node, module_aliases)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in TRUSTED_MODULE_CHAINS and alias.asname:
                    module_aliases.add(alias.asname)
    return bare_names, module_aliases


def _collect_shadowed_names(module_tree: ast.Module) -> set[str]:
    shadowed: set[str] = set()
    for node in module_tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            shadowed.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    shadowed.add(target.id)
    return shadowed


def _resolve_trusted_bindings(
    module_tree: ast.Module,
    *,
    module_path: str,
) -> TrustedBindings:
    bare_names, module_aliases = _collect_import_bindings(module_tree)
    bare_names -= _collect_shadowed_names(module_tree)

    partial_trusted = TrustedBindings(
        bare_names=frozenset(bare_names),
        module_aliases=frozenset(module_aliases),
    )
    for node in module_tree.body:
        if isinstance(node, ast.FunctionDef) and _is_gate_authorise_wrapper(
            node,
            partial_trusted,
        ):
            bare_names.add(node.name)

    if module_path.startswith(SYNTHETIC_MODULE_PREFIX) and not bare_names:
        bare_names.add("authorise")

    return TrustedBindings(
        bare_names=frozenset(bare_names),
        module_aliases=frozenset(module_aliases),
    )


def _attribute_chain(node: ast.expr) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _is_trusted_authorise_call(call: ast.Call, trusted: TrustedBindings) -> bool:
    if isinstance(call.func, ast.Name):
        return call.func.id in trusted.bare_names
    if isinstance(call.func, ast.Attribute) and call.func.attr == "authorise":
        if isinstance(call.func.value, ast.Name):
            return call.func.value.id in trusted.module_aliases
        chain = _attribute_chain(call.func.value)
        return chain in TRUSTED_MODULE_CHAINS
    return False


def _is_getattr_authorise_call(call: ast.Call) -> bool:
    if not isinstance(call.func, ast.Call):
        return False
    inner = call.func
    if not isinstance(inner.func, ast.Attribute) or inner.func.attr != "getattr":
        return False
    if len(inner.args) < 2:
        return False
    name_arg = inner.args[1]
    return isinstance(name_arg, ast.Constant) and name_arg.value == "authorise"


def _nearest_function_owner(
    node: ast.AST,
    parents: dict[ast.AST, ast.AST],
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = parents.get(current)
    return None


def _is_direct_module_child(
    owner: ast.FunctionDef | ast.AsyncFunctionDef,
    module_tree: ast.Module,
    parents: dict[ast.AST, ast.AST],
) -> bool:
    parent = parents.get(owner)
    return parent is module_tree and owner in module_tree.body


def _is_gate_authorise_wrapper(node: ast.FunctionDef, trusted: TrustedBindings) -> bool:
    if node.name != "authorise":
        return False
    policy_targets = {name for name in trusted.bare_names if name != "authorise"}
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and (
                child.func.id in policy_targets or child.func.id == "_policy_authorise"
            )
        ):
            return True
    return False


def _call_inside_node(call: ast.Call, container: ast.AST) -> bool:
    return any(child is call for child in ast.walk(container))


def _iter_module_calls(module_tree: ast.Module) -> Iterator[ast.Call]:
    for node in ast.walk(module_tree):
        if isinstance(node, ast.Call):
            yield node


def _call_has_exact_args(
    call: ast.Call,
    *,
    positional: tuple[str, ...],
    keywords: tuple[str, ...] = (),
) -> bool:
    if any(isinstance(arg, ast.Starred) for arg in call.args):
        return False
    if any(keyword.arg is None for keyword in call.keywords):
        return False
    positional_ok = len(call.args) == len(positional) and all(
        _is_name(argument, name=expected_name)
        for argument, expected_name in zip(call.args, positional, strict=True)
    )
    keywords_ok = len(call.keywords) == len(keywords) and all(
        keyword.arg == expected_name and _is_name(keyword.value, name=expected_name)
        for keyword, expected_name in zip(call.keywords, keywords, strict=True)
    )
    return positional_ok and keywords_ok


def _is_docstring(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def _top_level_statements(function_node: ast.AsyncFunctionDef) -> list[ast.stmt]:
    return [
        statement
        for statement in function_node.body
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _first_non_docstring_statement(
    function_node: ast.AsyncFunctionDef,
) -> ast.stmt | None:
    for statement in _top_level_statements(function_node):
        if _is_docstring(statement):
            continue
        return statement
    return None


def _is_direct_lock_await(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.Expr):
        return False
    expression = statement.value
    if not isinstance(expression, ast.Await):
        return False
    call = expression.value
    return (
        isinstance(call, ast.Call)
        and _is_direct_name_call(call, "lock_customer_for_update")
        and _call_has_exact_args(call, positional=("session", "customer_id"))
    )


def _has_direct_first_lock(function_node: ast.AsyncFunctionDef) -> bool:
    first_statement = _first_non_docstring_statement(function_node)
    return first_statement is not None and _is_direct_lock_await(first_statement)


def _is_propose_refund_type_guard(test: ast.expr) -> bool:
    if not isinstance(test, ast.Call) or not _is_direct_name_call(test, "isinstance"):
        return False
    if test.keywords:
        return False
    if len(test.args) != 2:
        return False
    action_arg, type_arg = test.args
    return _is_name(action_arg, name="action") and _is_name(
        type_arg, name="ProposeRefund"
    )


def _is_case_row_get_assignment(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
        return False
    target = statement.targets[0]
    value = statement.value
    if not isinstance(value, ast.Await):
        return False
    call = value.value
    return (
        _is_name(target, name="case_row")
        and isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and _is_name(call.func.value, name="session")
        and call.func.attr == "get"
        and not call.keywords
        and len(call.args) == 2
        and _is_name(call.args[0], name="CaseRow")
        and _is_name(call.args[1], name="case_id")
    )


def _is_case_not_found_guard(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.If) or statement.orelse:
        return False
    test = statement.test
    if not (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], (ast.Is, ast.Eq))
        and _is_name(test.left, name="case_row")
        and len(test.comparators) == 1
        and (
            (
                isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value is None
            )
            or _is_name(test.comparators[0], name="None")
        )
    ):
        return False
    if len(statement.body) != 1:
        return False
    raise_stmt = statement.body[0]
    if not isinstance(raise_stmt, ast.Raise) or raise_stmt.exc is None:
        return False
    exc = raise_stmt.exc
    return (
        isinstance(exc, ast.Call)
        and _is_direct_name_call(exc, "CaseNotFoundError")
        and not exc.keywords
        and len(exc.args) == 1
        and _is_name(exc.args[0], name="case_id")
    )


def _is_delegate_assignment(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
        return False
    if not _is_name(statement.targets[0], name="verdict"):
        return False
    value = statement.value
    if not isinstance(value, ast.Await):
        return False
    call = value.value
    return (
        isinstance(call, ast.Call)
        and _is_direct_name_call(call, "execute_propose_refund")
        and _call_has_exact_args(
            call,
            positional=("session", "refund_session_factory"),
            keywords=("case_row", "case_id", "action"),
        )
    )


def _is_finalize_return(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.Return) or statement.value is None:
        return False
    value = statement.value
    if not isinstance(value, ast.Await):
        return False
    call = value.value
    return (
        isinstance(call, ast.Call)
        and _is_direct_name_call(call, "_finalize_propose_refund_verdict")
        and _call_has_exact_args(call, positional=("session", "case_id", "verdict"))
    )


def _refund_guard_body_is_exact_skeleton(body: list[ast.stmt]) -> bool:
    if len(body) != 4:
        return False
    return (
        _is_case_row_get_assignment(body[0])
        and _is_case_not_found_guard(body[1])
        and _is_delegate_assignment(body[2])
        and _is_finalize_return(body[3])
    )


def _is_refund_session_factory_setup(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
        return False
    if not _is_name(statement.targets[0], name="refund_session_factory"):
        return False
    value = statement.value
    if not isinstance(value, ast.BoolOp) or not isinstance(value.op, ast.Or):
        return False
    if len(value.values) != 2:
        return False
    left, right = value.values
    if not _is_name(left, name="session_factory"):
        return False
    return (
        isinstance(right, ast.Call)
        and _is_direct_name_call(right, "session_factory_for")
        and not right.keywords
        and len(right.args) == 1
        and _is_name(right.args[0], name="session")
    )


def _refund_first_guard_statement(function_node: ast.AsyncFunctionDef) -> ast.If | None:
    statements = [
        statement
        for statement in _top_level_statements(function_node)
        if not _is_docstring(statement)
    ]
    index = 0
    if index < len(statements) and _is_refund_session_factory_setup(statements[index]):
        index += 1
    if index >= len(statements):
        return None
    candidate = statements[index]
    if not isinstance(candidate, ast.If):
        return None
    return candidate


def _has_compliant_refund_first_guard(function_node: ast.AsyncFunctionDef) -> bool:
    guard = _refund_first_guard_statement(function_node)
    if guard is None:
        return False
    if not _is_propose_refund_type_guard(guard.test):
        return False
    if guard.orelse:
        return False
    return _refund_guard_body_is_exact_skeleton(guard.body)


def _iter_calls_in_block(statements: list[ast.stmt]) -> Iterator[ast.Call]:
    def visit(node: ast.AST) -> Iterator[ast.Call]:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return
        if isinstance(node, ast.Call):
            yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            yield from visit(child)

    for statement in statements:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield from visit(statement)


def _function_has_call_to(
    function_node: ast.AsyncFunctionDef | ast.FunctionDef,
    name: str,
) -> bool:
    for call in _iter_calls_in_block(function_node.body):
        if _is_direct_name_call(call, name):
            return True
    return False


def _call_matches_direct_spec(
    node: ast.AST,
    *,
    name: str,
    positional: tuple[str, ...],
    keywords: tuple[str, ...],
    awaited: bool,
) -> bool:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    if awaited:
        if not isinstance(node, ast.Await):
            return False
        call = node.value
    elif isinstance(node, ast.Await) or not isinstance(node, ast.Call):
        return False
    else:
        call = node
    if not isinstance(call, ast.Call):
        return False
    if not _is_direct_name_call(call, name):
        return False
    return _call_has_exact_args(call, positional=positional, keywords=keywords)


def _function_has_direct_call(
    function_node: ast.AsyncFunctionDef,
    *,
    name: str,
    positional: tuple[str, ...],
    keywords: tuple[str, ...] = (),
    awaited: bool = True,
) -> bool:
    for statement in function_node.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(statement):
            if _call_matches_direct_spec(
                node,
                name=name,
                positional=positional,
                keywords=keywords,
                awaited=awaited,
            ):
                return True
    return False


def _trusted_call_lines(
    function_node: ast.AsyncFunctionDef,
    trusted: TrustedBindings,
) -> list[int]:
    return [
        call.lineno
        for call in _iter_calls_in_block(function_node.body)
        if _is_trusted_authorise_call(call, trusted)
    ]


def _context_load_lines(function_node: ast.AsyncFunctionDef) -> list[int]:
    return [
        call.lineno
        for call in _iter_calls_in_block(function_node.body)
        if _is_direct_name_call(call, "load_rule_context_for_case")
    ]


def _shape_a_authorise_order_offender(
    function_node: ast.AsyncFunctionDef,
    trusted: TrustedBindings,
) -> str | None:
    if not _has_direct_first_lock(function_node):
        return None
    context_lines = _context_load_lines(function_node)
    if not context_lines:
        return "Shape A owner missing load_rule_context_for_case"
    min_context_line = min(context_lines)
    for line in _trusted_call_lines(function_node, trusted):
        if line < min_context_line:
            return (
                "trusted authorise call must not precede "
                "load_rule_context_for_case in Shape A owners"
            )
    return None


def _untrusted_authorise_attribute_offender(
    function_node: ast.AsyncFunctionDef,
    trusted: TrustedBindings,
) -> str | None:
    for call in _iter_calls_in_block(function_node.body):
        if not isinstance(call.func, ast.Attribute) or call.func.attr != "authorise":
            continue
        if _is_trusted_authorise_call(call, trusted):
            continue
        return "untrusted attribute authorise call is not permitted"
    return None


def _evaluator_shape_offender(
    function_node: ast.AsyncFunctionDef,
    trusted: TrustedBindings,
) -> str | None:
    attribute_offender = _untrusted_authorise_attribute_offender(function_node, trusted)
    if attribute_offender is not None:
        return attribute_offender
    if _has_direct_first_lock(function_node):
        order_offender = _shape_a_authorise_order_offender(function_node, trusted)
        if order_offender is not None:
            return order_offender
        return None
    if _has_compliant_refund_first_guard(function_node):
        return None
    return (
        "must either begin with await lock_customer_for_update(session, customer_id) "
        "or begin with isinstance(action, ProposeRefund) guard matching the exact "
        "production refund-first skeleton"
    )


def _owner_classification_verdict(
    owner: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    owner_key: OwnerKey,
    module_tree: ast.Module,
    parents: dict[ast.AST, ast.AST],
    trusted: TrustedBindings,
) -> str | None:
    if not isinstance(owner, ast.AsyncFunctionDef):
        return f"{owner_key.label()}: sync owner is not permitted"
    if not _is_direct_module_child(owner, module_tree, parents):
        return (
            f"{owner_key.label()}: unsanctioned nested owner; only direct "
            "module-child async def owners are permitted"
        )
    shape_offender = _evaluator_shape_offender(owner, trusted)
    if shape_offender is None:
        return None
    return f"{owner_key.label()}: {shape_offender}"


def _context_loader_authorise_offender(
    node: ast.AsyncFunctionDef,
    *,
    owner_key: OwnerKey,
    trusted: TrustedBindings,
) -> str | None:
    for call in _iter_calls_in_block(node.body):
        if _is_getattr_authorise_call(call):
            return (
                f"{owner_key.label()}: dynamic authorise access via getattr "
                "is outside the static proof"
            )
        if (
            isinstance(call.func, ast.Name)
            and call.func.id == "authorise"
            and not _is_trusted_authorise_call(call, trusted)
        ):
            return f"{owner_key.label()}: untrusted or shadowed authorise call"
        if (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "authorise"
            and not _is_trusted_authorise_call(call, trusted)
        ):
            return (
                f"{owner_key.label()}: untrusted attribute authorise call "
                "is not permitted"
            )
    return None


def _analyze_module_money_policy(
    module_path: str,
    module_tree: ast.Module,
) -> list[str]:
    """Single aggregation pass: trusted call site -> owner -> shape verdict."""
    parents = _build_parent_map(module_tree)
    trusted = _resolve_trusted_bindings(module_tree, module_path=module_path)
    offenders: list[str] = []
    classified: dict[OwnerKey, str | None] = {}

    gate_wrappers = [
        node
        for node in module_tree.body
        if isinstance(node, ast.FunctionDef)
        and _is_gate_authorise_wrapper(node, trusted)
    ]

    for call in _iter_module_calls(module_tree):
        if any(_call_inside_node(call, wrapper) for wrapper in gate_wrappers):
            continue
        if not _is_trusted_authorise_call(call, trusted):
            continue

        owner = _nearest_function_owner(call, parents)
        if owner is None:
            offenders.append(
                f"{module_path}:<module>:{call.lineno}: module-level trusted "
                "authorise call is not permitted",
            )
            continue

        owner_key = _owner_key(
            owner,
            module_path=module_path,
            parents=parents,
            module_tree=module_tree,
            lineno=owner.lineno,
        )
        if owner_key in classified:
            continue
        classified[owner_key] = _owner_classification_verdict(
            owner,
            owner_key=owner_key,
            module_tree=module_tree,
            parents=parents,
            trusted=trusted,
        )

    for node in module_tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if not _function_has_call_to(node, "load_rule_context_for_case"):
            continue
        owner_key = _owner_key(
            node,
            module_path=module_path,
            parents=parents,
            module_tree=module_tree,
            lineno=node.lineno,
        )
        if owner_key in classified:
            continue
        classified[owner_key] = _context_loader_authorise_offender(
            node,
            owner_key=owner_key,
            trusted=trusted,
        )

    offenders.extend(verdict for verdict in classified.values() if verdict is not None)
    return offenders


def _locked_reevaluation_chain_offenders() -> list[str]:
    refund_module_path = GATE_DIRECTORY / "refund.py"
    refund_tree = ast.parse(refund_module_path.read_text(encoding="utf-8"))
    parents = _build_parent_map(refund_tree)

    execute_node = next(
        node
        for node in refund_tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == LOCKED_REEVALUATION_ENTRY
    )
    offenders: list[str] = []
    if not _function_has_direct_call(
        execute_node,
        name=LOCKED_DECISIVE_EVALUATOR,
        positional=("session",),
        keywords=("customer_id", "case_id", "action"),
    ):
        offenders.append(
            f"{LOCKED_REEVALUATION_ENTRY} must await {LOCKED_DECISIVE_EVALUATOR}"
            "(session, customer_id=customer_id, case_id=case_id, action=action)",
        )

    decisive_node = next(
        node
        for node in refund_tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == LOCKED_DECISIVE_EVALUATOR
    )
    if not _has_direct_first_lock(decisive_node):
        offenders.append(
            f"{LOCKED_DECISIVE_EVALUATOR}: first statement must be "
            "await lock_customer_for_update(session, customer_id)",
        )
    _ = parents
    return offenders


def _iter_gate_modules() -> list[tuple[str, ast.Module]]:
    modules: list[tuple[str, ast.Module]] = []
    for module_path in sorted(GATE_DIRECTORY.rglob("*.py")):
        if module_path.name == "__init__.py":
            continue
        relative_path = module_path.relative_to(SOURCE_ROOT).as_posix()
        modules.append(
            (
                relative_path,
                ast.parse(module_path.read_text(encoding="utf-8")),
            ),
        )
    return modules


def _analyze_gate_money_policy() -> list[str]:
    offenders: list[str] = []
    compliant_count = 0
    for module_path, module_tree in _iter_gate_modules():
        module_offenders = _analyze_module_money_policy(module_path, module_tree)
        offenders.extend(module_offenders)
        parents = _build_parent_map(module_tree)
        trusted = _resolve_trusted_bindings(module_tree, module_path=module_path)
        gate_wrappers = [
            node
            for node in module_tree.body
            if isinstance(node, ast.FunctionDef)
            and _is_gate_authorise_wrapper(node, trusted)
        ]
        for call in _iter_module_calls(module_tree):
            if any(_call_inside_node(call, wrapper) for wrapper in gate_wrappers):
                continue
            if not _is_trusted_authorise_call(call, trusted):
                continue
            owner = _nearest_function_owner(call, parents)
            if (
                owner is not None
                and isinstance(owner, ast.AsyncFunctionDef)
                and _is_direct_module_child(owner, module_tree, parents)
                and _evaluator_shape_offender(owner, trusted) is None
            ):
                compliant_count += 1

    offenders.extend(_locked_reevaluation_chain_offenders())
    if compliant_count == 0:
        offenders.append(
            f"{STRUCTURE_EVIDENCE_LABEL}: expected at least one compliant "
            "money-policy owner",
        )
    return offenders


def _assert_synthetic_compliance(
    source: str,
    *,
    module_path: str,
    should_pass: bool,
) -> None:
    tree = ast.parse(source)
    offenders = _analyze_module_money_policy(module_path, tree)
    trusted_calls = [
        call
        for call in _iter_module_calls(tree)
        if _is_trusted_authorise_call(
            call,
            _resolve_trusted_bindings(tree, module_path=module_path),
        )
    ]
    if should_pass:
        assert offenders == [], f"expected compliance, got: {offenders}"
        assert trusted_calls, "expected at least one trusted authorise call"
    else:
        assert offenders or not trusted_calls, (
            f"unsafe source incorrectly accepted: offenders={offenders} "
            f"trusted_calls={len(trusted_calls)}"
        )
        if trusted_calls:
            assert offenders, "trusted calls discovered but no offenders reported"


def test_mutation_guard_rejects_unlocked_synthetic_evaluator() -> None:
    source = """
async def unlocked_future_money_path(session, case_id, action):
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)
"""
    tree = ast.parse(source)
    module_path = "synthetic/unlocked.py"
    offenders = _analyze_module_money_policy(module_path, tree)
    assert offenders, f"unlocked evaluator must be rejected: {offenders}"
    assert any("unlocked_future_money_path" in offender for offender in offenders), (
        f"unlocked evaluator must be rejected by compliance: {offenders}"
    )


def test_mutation_guard_accepts_locked_synthetic_evaluator() -> None:
    _assert_synthetic_compliance(
        LOCKED_EVALUATOR,
        module_path="synthetic/locked.py",
        should_pass=True,
    )


def test_mutation_guard_accepts_refund_dispatch_before_authorise() -> None:
    _assert_synthetic_compliance(
        PRODUCTION_SAFE_REFUND_ROUTER,
        module_path="synthetic/refund_first.py",
        should_pass=True,
    )


def test_mutation_guard_ignores_nested_authorise_for_outer_compliance() -> None:
    module_path = "synthetic/nested_noise.py"
    tree = ast.parse(NESTED_AUTHORISE_NOISE_ROUTER)
    offenders = _analyze_module_money_policy(module_path, tree)
    assert not any(
        offender.startswith(f"{module_path}:outer_with_nested_noise:")
        for offender in offenders
    ), f"outer router must pass despite nested authorise: {offenders}"


def test_authorise_site_without_context_load_is_not_an_evaluator() -> None:
    source = """
async def policy_only(session, case_id, action):
    return authorise(rule_context, action)
"""
    module_path = "synthetic/policy_only.py"
    tree = ast.parse(source)
    offenders = _analyze_module_money_policy(module_path, tree)
    assert offenders, "bare authorise without context load must still be rejected"


def test_nested_function_authorise_attributed_to_distinct_owners() -> None:
    source = """
async def outer_router(session, refund_session_factory, case_id, action):
    async def nested_helper():
        _, rule_context = await load_rule_context_for_case(session, case_id)
        return authorise(rule_context, action)

    _ = nested_helper
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
    module_path = "synthetic/nested_attribution.py"
    tree = ast.parse(source)
    parents = _build_parent_map(tree)
    trusted = _resolve_trusted_bindings(tree, module_path=module_path)
    owners: set[tuple[str, ...]] = set()
    for call in _iter_module_calls(tree):
        if not _is_trusted_authorise_call(call, trusted):
            continue
        owner = _nearest_function_owner(call, parents)
        if owner is not None:
            owners.add(_qualified_owner_path(owner, parents, tree))
    assert owners == {("outer_router", "nested_helper"), ("outer_router",)}


def test_mutation_guards_reject_unsafe_evaluator_shapes(
    mutation_key: str,
    source: str,
) -> None:
    _assert_synthetic_compliance(
        source,
        module_path=f"synthetic/{mutation_key}.py",
        should_pass=False,
    )


test_mutation_guards_reject_unsafe_evaluator_shapes = pytest.mark.parametrize(
    ("mutation_key", "source"),
    REJECTED_MUTATIONS,
    ids=[mutation_key for mutation_key, _ in REJECTED_MUTATIONS],
)(test_mutation_guards_reject_unsafe_evaluator_shapes)


def test_gate_money_policy_structure_contract() -> None:
    """Every trusted authorise owner in gate modules matches a safe shape."""
    offenders = _analyze_gate_money_policy()
    assert offenders == [], (
        f"{STRUCTURE_EVIDENCE_LABEL}: money-policy structure offenders: {offenders}"
    )


@pytest.mark.parametrize(
    ("function_name", "source", "reason"),
    [
        (
            "wrong_context_ignored",
            """
async def wrong_context_ignored(session, case_id, action):
    _, ctx = await load_rule_context_for_case(session, case_id)
    return authorise(ctx, action)
""",
            "shape",
        ),
        (
            "wrong_action_ignored",
            """
async def wrong_action_ignored(session, case_id, action):
    _, rule_context = await load_rule_context_for_case(session, case_id)
    generated = action
    return authorise(rule_context, generated)
""",
            "shape",
        ),
        (
            "aliased_policy_ignored",
            """
from saferefund.policy.policy import authorise as policy_authorise

async def aliased_policy_ignored(session, case_id, action):
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return policy_authorise(rule_context, action)
""",
            "shape",
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
            "shape",
        ),
    ],
)
def test_manager_probe_mutations_fail_for_intended_reason(
    function_name: str,
    source: str,
    reason: str,
) -> None:
    module_path = f"synthetic/{function_name}.py"
    tree = ast.parse(source)
    trusted = _resolve_trusted_bindings(tree, module_path=module_path)
    trusted_call_count = sum(
        1
        for call in _iter_module_calls(tree)
        if _is_trusted_authorise_call(call, trusted)
    )
    offenders = _analyze_module_money_policy(module_path, tree)
    assert trusted_call_count >= 1, (
        f"{function_name} trusted call vanished from discovery"
    )
    assert offenders, f"{function_name} must fail {reason}, not vanish: {offenders}"


def test_module_qualified_policy_alias_is_discovered_and_rejected() -> None:
    source = """
import saferefund.policy.policy as policy_module

async def module_qualified_policy_alias(session, case_id, action):
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return policy_module.authorise(rule_context, action)
"""
    _assert_synthetic_compliance(
        source,
        module_path="synthetic/module_qualified_policy_alias.py",
        should_pass=False,
    )


def test_attacker_authorise_is_not_trusted_primitive() -> None:
    source = """
async def attacker_authorise_not_trusted(session, case_id, action, attacker):
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return attacker.authorise(rule_context, action)
"""
    module_path = "synthetic/attacker_authorise_not_trusted.py"
    tree = ast.parse(source)
    trusted = _resolve_trusted_bindings(tree, module_path=module_path)
    trusted_calls = [
        call
        for call in _iter_module_calls(tree)
        if _is_trusted_authorise_call(call, trusted)
    ]
    assert trusted_calls == []
    offenders = _analyze_module_money_policy(module_path, tree)
    assert offenders, "untrusted attacker.authorise must not pass silently"


def test_colliding_nested_resolves_distinct_owner_paths() -> None:
    source = """
async def shared(session, customer_id, case_id, action):
    await lock_customer_for_update(session, customer_id)
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return authorise(rule_context, action)

async def outer_router(session, case_id, action):
    async def shared():
        _, rule_context = await load_rule_context_for_case(session, case_id)
        return authorise(rule_context, action)
    return shared
"""
    module_path = "synthetic/colliding_nested.py"
    tree = ast.parse(source)
    offenders = _analyze_module_money_policy(module_path, tree)
    assert any("outer_router.shared" in offender for offender in offenders), offenders
    assert not any(
        offender.startswith("synthetic/colliding_nested.py:shared:3")
        for offender in offenders
    ), f"top-level shared must not be blamed for nested owner: {offenders}"


def test_gate_operations_module_alias_import_is_discovered_and_rejected() -> None:
    """Counterexample: import gate.operations as module object, not from-import."""
    source = """
import saferefund.gate.operations as gate_ops

async def gate_operations_module_alias(session, case_id, action):
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return gate_ops.authorise(rule_context, action)
"""
    module_path = "synthetic/gate_operations_module_alias.py"
    tree = ast.parse(source)
    trusted = _resolve_trusted_bindings(tree, module_path=module_path)
    assert any(
        _is_trusted_authorise_call(call, trusted) for call in _iter_module_calls(tree)
    ), "gate module-object alias import must be discovered"
    offenders = _analyze_module_money_policy(module_path, tree)
    assert offenders, offenders


def test_gate_operations_dotted_import_is_discovered_and_rejected() -> None:
    """Unaliased module import binds saferefund; full chain must be discovered."""
    source = """
import saferefund.gate.operations

async def gate_operations_dotted_import(session, case_id, action):
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return saferefund.gate.operations.authorise(rule_context, action)
"""
    module_path = "synthetic/gate_operations_dotted_import.py"
    tree = ast.parse(source)
    trusted = _resolve_trusted_bindings(tree, module_path=module_path)
    trusted_calls = [
        call
        for call in _iter_module_calls(tree)
        if _is_trusted_authorise_call(call, trusted)
    ]
    assert trusted_calls, "dotted gate module import must discover full-chain authorise"
    offenders = _analyze_module_money_policy(module_path, tree)
    assert offenders, offenders


def test_gate_operations_package_parent_import_is_discovered_and_rejected() -> None:
    """Package-parent module-object import must bind operations, not saferefund."""
    source = """
from saferefund.gate import operations

async def gate_operations_package_parent(session, case_id, action):
    _, rule_context = await load_rule_context_for_case(session, case_id)
    return operations.authorise(rule_context, action)
"""
    module_path = "synthetic/gate_operations_package_parent.py"
    tree = ast.parse(source)
    trusted = _resolve_trusted_bindings(tree, module_path=module_path)
    trusted_calls = [
        call
        for call in _iter_module_calls(tree)
        if _is_trusted_authorise_call(call, trusted)
    ]
    assert trusted_calls, (
        "package-parent gate operations import must discover operations.authorise"
    )
    offenders = _analyze_module_money_policy(module_path, tree)
    assert offenders, offenders

"""Gate cleanup, CaseSummary leak removal, and INTERNAL_SIDE_EFFECT label."""

from __future__ import annotations

import ast
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from saferefund.actions.models import Escalate
from saferefund.agent.prompt import build_prompt
from saferefund.domain.enums import Actor, CaseStatus
from saferefund.domain.events import EventType
from saferefund.policy.checks import ACTION_OBLIGATIONS
from saferefund.projections.case import CaseSummary, project_case_summary
from tests.unit.labels_helpers import Label, labels_for_action
from tests.unit.policy_helpers import customer_summary, open_case_summary
from tests.unit.projection_helpers import fold_event

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
REPO_ROOT = SOURCE_ROOT.parent
GATE_ROOT = SOURCE_ROOT / "saferefund" / "gate"
GATE_CYCLE_MODULES = (
    GATE_ROOT / "operations.py",
    GATE_ROOT / "effects.py",
    GATE_ROOT / "refund.py",
)


def _module_level_imports(module_path: Path) -> set[tuple[str, str]]:
    module_tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imports: set[tuple[str, str]] = set()
    for node in ast.walk(module_tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imports.add((node.module, alias.name))
    return imports


def _function_local_gate_imports(module_path: Path) -> list[str]:
    module_tree = ast.parse(module_path.read_text(encoding="utf-8"))
    return [
        f"{module_path.relative_to(SOURCE_ROOT)}:{child.lineno}:{child.module}"
        for node in ast.walk(module_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for child in ast.walk(node)
        if isinstance(child, ast.ImportFrom)
        and child.module
        and child.module.startswith("saferefund.gate.")
    ]


def _gate_cycle_edges() -> dict[str, set[str]]:
    module_names = {path.stem: path for path in GATE_CYCLE_MODULES}
    edges: dict[str, set[str]] = {name: set() for name in module_names}
    for name, path in module_names.items():
        for imported_module, _imported_name in _module_level_imports(path):
            if imported_module == "saferefund.gate.operations":
                edges[name].add("operations")
            elif imported_module == "saferefund.gate.effects":
                edges[name].add("effects")
            elif imported_module == "saferefund.gate.refund":
                edges[name].add("refund")
    return edges


def _assert_acyclic(edges: dict[str, set[str]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, trail: list[str]) -> None:
        if node in visiting:
            cycle = " -> ".join([*trail, node])
            pytest.fail(f"gate import cycle detected: {cycle}")
        if node in visited:
            return
        visiting.add(node)
        for neighbour in edges[node]:
            visit(neighbour, [*trail, node])
        visiting.remove(node)
        visited.add(node)

    for module_name in edges:
        visit(module_name, [])


def test_operator_imports_public_refund_helpers_not_underscore_aliases() -> None:
    """Mutation: import ``_append_refund_approved`` in ``gate/operator.py``."""
    operator_tree = ast.parse((GATE_ROOT / "operator.py").read_text(encoding="utf-8"))
    offenders = [
        f"{alias.name}:{alias.asname or alias.name}"
        for node in operator_tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "saferefund.gate.refund"
        for alias in node.names
        if alias.name.startswith("_")
    ]
    assert offenders == []


def test_gate_modules_have_no_function_local_import_cycle_workarounds() -> None:
    """Mutation: add a deferred gate.refund import inside a helper."""
    offenders = [
        site
        for module_path in GATE_CYCLE_MODULES
        for site in _function_local_gate_imports(module_path)
    ]
    assert offenders == []


def test_operations_effects_refund_module_graph_has_no_import_cycle() -> None:
    """Mutation: import ``operations`` from ``refund.py`` at module level."""
    edges = _gate_cycle_edges()
    _assert_acyclic(edges)
    assert edges["refund"].isdisjoint({"operations", "effects"})
    assert edges["effects"].isdisjoint({"operations", "refund"})


def test_dispatch_allow_verdict_does_not_union_propose_refund_with_escalate() -> None:
    """Mutation: restore ``Escalate() | ProposeRefund()`` in ``effects.py``."""
    effects_source = (GATE_ROOT / "effects.py").read_text(encoding="utf-8")
    assert "Escalate() | ProposeRefund()" not in effects_source
    assert "ProposeRefund must be dispatched by execute_agent_action." in effects_source


def test_case_summary_public_surface_excludes_token_and_pending_refund_fields() -> None:
    """Mutation: add ``verification_token`` back to ``CaseSummary``."""
    public_fields = set(CaseSummary.__dataclass_fields__)
    assert public_fields.isdisjoint(
        {"pending_refund_id", "verification_token", "verification_expires_at"},
    )


def test_build_prompt_state_and_text_do_not_surface_verification_token() -> None:
    """Mutation: project ``verification_token`` onto ``CaseSummary`` again."""
    expires_at = datetime(2030, 1, 15, 10, 0, tzinfo=UTC)
    case_summary = project_case_summary(
        case_id="case_token_leak",
        customer_id="cust_sophie",
        events=[
            fold_event(
                seq=1,
                event_type=EventType.VERIFICATION_REQUESTED,
                actor=Actor.AGENT,
                case_id="case_token_leak",
                payload={"token": "vtok_live_secret", "expires_at": expires_at},
            ),
        ],
        now=expires_at - timedelta(minutes=1),
    )
    assert case_summary.status is CaseStatus.AWAITING_VERIFICATION

    prompt = build_prompt(
        case_summary,
        customer_summary(verified=False),
        [],
        [],
    )
    serialized_state = json.dumps(
        prompt.state,
        default=lambda value: getattr(value, "value", str(value)),
    )
    assert "vtok_live_secret" not in serialized_state
    assert "vtok_live_secret" not in prompt.text


def test_loop_filters_case_events_build_prompt_does_not() -> None:
    """Mutation: reintroduce case_id filtering inside build_prompt."""
    loop_source = (SOURCE_ROOT / "saferefund" / "agent" / "loop.py").read_text(
        encoding="utf-8",
    )
    prompt_source = (SOURCE_ROOT / "saferefund" / "agent" / "prompt.py").read_text(
        encoding="utf-8",
    )
    assert "event.case_id == case_id" in loop_source
    assert "event.case_id == case_summary.case_id" not in prompt_source


def test_escalate_label_is_internal_side_effect_without_policy_obligation_change() -> (
    None
):
    """Mutation: route Escalate through INTERNAL_SIDE_EFFECT in ACTION_OBLIGATIONS."""
    escalate = Escalate(action="escalate", reason="human needed")
    assert labels_for_action(escalate) == frozenset({Label.INTERNAL_SIDE_EFFECT})
    obligations = ACTION_OBLIGATIONS[Escalate]
    assert obligations.required == frozenset()
    assert obligations.rationale.strip() != ""


def test_saferefund_db_is_ignored_local_artifact_not_tracked() -> None:
    """Mutation: ``git add saferefund.db``; tracked-file assertion fails."""
    gitignore_lines = (
        (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    )
    assert "saferefund.db" in gitignore_lines
    assert "*.sqlite" in gitignore_lines
    assert "*.sqlite3" in gitignore_lines

    tracked = subprocess.run(
        ["/usr/bin/git", "ls-files", "--error-unmatch", "saferefund.db"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    assert tracked.returncode != 0, (
        "saferefund.db must not be tracked in version control"
    )


def test_open_case_summary_helper_matches_public_projection_fields() -> None:
    """Mutation: add ``pending_refund_id`` back to ``open_case_summary()``."""
    summary = open_case_summary()
    assert not hasattr(summary, "pending_refund_id")
    assert not hasattr(summary, "verification_token")
    assert not hasattr(summary, "verification_expires_at")

"""Gate layering hygiene and runtime adapter call-origin enforcement.

The AST scans below check import direction and naming across **every** gate module
and adapter imports across **every** discovered non-gate production module. They
are layering checks only: they do not stop ``importlib``, ``getattr``, or runtime
rebinding bypasses. The integration autouse fixture in ``tests/integration/conftest.py``
exercises the runtime call-origin property across every scenario; the companion
test below rejects unmediated immediate callers without re-running the full
integration matrix.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.integration import adapter_mediation
from tests.invariants import adapter_import_mutations, adapter_import_scanner

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
MEDIATED_LAYERS = ("agent", "api")
GATE_DIRECTORY = SOURCE_ROOT / "saferefund" / "gate"

GATE_FACADE_MODULES = frozenset(
    {
        "saferefund.gate",
        "saferefund.gate.operations",
        "saferefund.gate.outcomes",
    }
)

GATE_INTERNAL_MODULES = frozenset(
    {
        "saferefund.gate.common",
        "saferefund.gate.effects",
        "saferefund.gate.refund",
        "saferefund.gate.operator",
        "saferefund.gate.verification",
    }
)

MEDIATED_GATE_OPERATIONS = frozenset(
    {
        "approve_refund",
        "confirm_verification",
        "escalate_case",
        "expire_due_refunds_for_customer",
        "execute_agent_action",
        "reject_refund",
        "send_unknown_sender_reply",
    }
)


def _module_level_function_names(module_path: Path) -> set[str]:
    module_tree = ast.parse(module_path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in module_tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
    return names


def _imported_gate_modules(module_path: Path) -> set[str]:
    module_tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(module_tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module.startswith("saferefund.gate"):
                imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(
                alias.name
                for alias in node.names
                if alias.name.startswith("saferefund.gate")
            )
    return imported


def _mediated_layer_modules() -> list[Path]:
    return [
        module_path
        for layer in MEDIATED_LAYERS
        for module_path in sorted((SOURCE_ROOT / "saferefund" / layer).rglob("*.py"))
    ]


def _non_gate_source_modules() -> list[Path]:
    gate_root = SOURCE_ROOT / "saferefund" / "gate"
    return [
        module_path
        for module_path in sorted((SOURCE_ROOT / "saferefund").rglob("*.py"))
        if not module_path.is_relative_to(gate_root)
    ]


def test_untrusted_layers_import_only_the_gate_facade() -> None:
    """Agent and API code must reach the gate through its façade modules only.

    Mutation that turns red: add
    ``from saferefund.gate.effects import allow_send_reply`` to ``agent/loop.py``.
    """
    violations = {
        str(module_path.relative_to(SOURCE_ROOT)): sorted(
            imported_module
            for imported_module in _imported_gate_modules(module_path)
            if imported_module not in GATE_FACADE_MODULES
        )
        for module_path in _mediated_layer_modules()
    }
    non_facade_imports = {
        module: imports for module, imports in violations.items() if imports
    }
    assert non_facade_imports == {}


def test_no_gate_module_exposes_a_public_effect_helper() -> None:
    """Effect helpers must use underscored names in every gate module.

    Layering check only — does not survive ``importlib``. Mutation that turns red:
    rename ``_allow_send_reply`` back to ``allow_send_reply``.
    """
    offenders = {
        f"{path.relative_to(GATE_DIRECTORY)}:{name}"
        for path in GATE_DIRECTORY.rglob("*.py")
        for name in _module_level_function_names(path)
        if name.startswith(("allow_", "dispatch_"))
    }
    assert offenders == set()


def test_no_module_outside_the_gate_imports_a_gate_internal() -> None:
    """Every non-gate module must use the façade, not gate internals.

    Layering check only — does not survive ``importlib``. Mutation that turns red:
    ``from saferefund.gate.demo_support import snapshot_outbox`` in ``demo.py``.
    """
    violations = {
        str(module_path.relative_to(SOURCE_ROOT)): sorted(
            imported_module
            for imported_module in _imported_gate_modules(module_path)
            if imported_module in GATE_INTERNAL_MODULES
        )
        for module_path in _non_gate_source_modules()
    }
    non_facade_imports = {
        module: imports for module, imports in violations.items() if imports
    }
    assert non_facade_imports == {}


def test_gate_facade_exports_exactly_the_mediated_operations() -> None:
    """Mutation: export a private effect helper through ``gate.__all__``."""
    import saferefund.gate as gate_facade

    facade_callables = {
        name
        for name in getattr(gate_facade, "__all__", ())
        if callable(getattr(gate_facade, name, None))
        and not isinstance(getattr(gate_facade, name), type)
    }
    assert facade_callables == MEDIATED_GATE_OPERATIONS


def test_integration_autouse_adapter_mediation_fixture_is_registered() -> None:
    """Mutation: delete the autouse mediation fixture from integration conftest."""
    import inspect

    from _pytest.fixtures import FixtureFunctionDefinition

    from tests.integration import conftest as integration_conftest

    fixture = integration_conftest._every_adapter_call_is_mediated  # noqa: SLF001
    assert isinstance(fixture, FixtureFunctionDefinition)
    assert "@pytest.fixture(autouse=True)" in inspect.getsource(fixture)


def test_runtime_adapter_call_origin_rejects_unmediated_immediate_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: bless any deeper gate frame instead of the immediate caller."""
    stack = [
        SimpleNamespace(filename=adapter_mediation.__file__, lineno=1),
        SimpleNamespace(filename=adapter_mediation.__file__, lineno=2),
        SimpleNamespace(filename="src/saferefund/agent/bypass.py", lineno=3),
        SimpleNamespace(filename="src/saferefund/gate/effects.py", lineno=4),
    ]
    monkeypatch.setattr(adapter_mediation.inspect, "stack", lambda: stack)

    with pytest.raises(AssertionError, match="outside gate mediation"):
        adapter_mediation._assert_adapter_call_is_mediated()  # noqa: SLF001


def test_every_non_gate_production_module_is_scanned_for_adapter_imports() -> None:
    """Mutation: add ``saferefund.bypass`` importing ``saferefund.adapters.mailer``."""
    violations = adapter_import_scanner.scan_all_non_gate_production_modules()
    offenders = {
        module: [f"{item.lineno}:{item.detail}" for item in module_violations]
        for module, module_violations in violations.items()
        if module_violations
    }
    assert offenders == {}


@pytest.mark.parametrize(
    ("mutation_id", "module_name", "source"),
    adapter_import_mutations.REJECTED_ADAPTER_IMPORT_MUTATIONS,
)
def test_rejected_adapter_import_mutations_are_reported_by_scanner(
    mutation_id: str,
    module_name: str,
    source: str,
) -> None:
    """Permanent rejected mutations for function-local and demo adapter bypasses."""
    violations = adapter_import_scanner.scan_module_source(module_name, source)
    assert violations, mutation_id
    if mutation_id.startswith("demo_"):
        assert any(
            "operation-specific" in violation.detail for violation in violations
        ), mutation_id
    if "send" in mutation_id or mutation_id == "synthetic_future_module_adapter_import":
        assert any("send" in violation.detail for violation in violations), mutation_id


def test_importlinter_adapter_mediation_overlap_is_documented_not_exhaustive() -> None:
    """import-linter overlap is layering hygiene; the AST scanner is exhaustive."""
    assert adapter_import_scanner.importlinter_adapter_mediation_source_roots() == [
        "saferefund",
    ]
    assert adapter_import_scanner.importlinter_adapter_mediation_forbidden_roots() == [
        "saferefund.adapters",
    ]
    has_source_forbidden_overlap = (
        adapter_import_scanner.importlinter_adapter_mediation_has_source_forbidden_overlap
    )()
    assert has_source_forbidden_overlap
    violations = adapter_import_scanner.scan_module_source(
        "saferefund.bypass",
        adapter_import_mutations.SYNTHETIC_FUTURE_MODULE_ADAPTER_IMPORT,
    )
    assert violations

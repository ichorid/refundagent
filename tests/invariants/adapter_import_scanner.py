"""AST scanner for adapter imports across discovered production modules.

Layering hygiene only: static analysis of import statements and adapter attribute
access does not stop ``importlib`` or runtime rebinding. The integration
call-origin fixture exercises runtime mediation separately.
"""

from __future__ import annotations

import ast
import configparser
from dataclasses import dataclass, field
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
REPOSITORY_ROOT = SOURCE_ROOT.parent
PACKAGE_ROOT = SOURCE_ROOT / "saferefund"
IMPORTLINTER_CONFIG = REPOSITORY_ROOT / ".importlinter"

ADAPTERS_PACKAGE = "saferefund.adapters"
GATE_PACKAGE = "saferefund.gate"
DEMO_MODULE = "saferefund.demo"

DEMO_ALLOWED_ADAPTER_IMPORTS: frozenset[tuple[str, str]] = frozenset(
    {
        (ADAPTERS_PACKAGE, "reset_adapters_for_tests"),
        ("saferefund.adapters.mailer", "snapshot_outbox"),
    },
)

FORBIDDEN_ADAPTER_OPERATIONS = frozenset({"send", "refund", "escalate"})
ADAPTER_SUBMODULE_SHORT_NAMES = frozenset({"mailer", "payment", "ticketing"})

IMPORTLINTER_EXEMPT_MODULES = frozenset({GATE_PACKAGE, ADAPTERS_PACKAGE})


@dataclass(frozen=True, slots=True)
class AdapterImportViolation:
    """One adapter layering violation in a production module."""

    module: str
    lineno: int
    detail: str


@dataclass
class _LexicalScope:
    bindings: dict[str, str] = field(default_factory=dict)
    parent: _LexicalScope | None = None

    def resolve(self, name: str) -> str | None:
        scope: _LexicalScope | None = self
        while scope is not None:
            if name in scope.bindings:
                return scope.bindings[name]
            scope = scope.parent
        return None


def path_to_module_name(module_path: Path) -> str:
    relative = module_path.relative_to(PACKAGE_ROOT)
    parts = list(relative.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1].removesuffix(".py")
    if not parts:
        return "saferefund"
    return f"saferefund.{'.'.join(parts)}"


def discover_production_module_paths() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def discover_production_module_names() -> list[str]:
    return [path_to_module_name(path) for path in discover_production_module_paths()]


def is_gate_module(module_name: str) -> bool:
    return module_name == GATE_PACKAGE or module_name.startswith(f"{GATE_PACKAGE}.")


def is_adapters_module(module_name: str) -> bool:
    return module_name == ADAPTERS_PACKAGE or module_name.startswith(
        f"{ADAPTERS_PACKAGE}.",
    )


def non_gate_production_modules() -> list[tuple[str, Path]]:
    modules: list[tuple[str, Path]] = []
    for module_path in discover_production_module_paths():
        module_name = path_to_module_name(module_path)
        if is_gate_module(module_name) or is_adapters_module(module_name):
            continue
        modules.append((module_name, module_path))
    return modules


def _is_adapter_module(module: str | None) -> bool:
    if module is None:
        return False
    return module == ADAPTERS_PACKAGE or module.startswith(f"{ADAPTERS_PACKAGE}.")


def _demo_import_allowed(imported_module: str, imported_name: str) -> bool:
    return (imported_module, imported_name) in DEMO_ALLOWED_ADAPTER_IMPORTS


def _record_import_violation(
    module_name: str,
    node: ast.AST,
    detail: str,
    violations: list[AdapterImportViolation],
) -> None:
    violations.append(
        AdapterImportViolation(
            module=module_name,
            lineno=getattr(node, "lineno", 0),
            detail=detail,
        ),
    )


def _register_binding(scope: _LexicalScope, local_name: str, target: str) -> None:
    scope.bindings[local_name] = target


def _child_scope(parent: _LexicalScope) -> _LexicalScope:
    return _LexicalScope(parent=parent)


def _is_adapter_binding(target: str) -> bool:
    return target == ADAPTERS_PACKAGE or target.startswith(f"{ADAPTERS_PACKAGE}.")


def _is_forbidden_adapter_operation_binding(target: str) -> bool:
    operation = target.rsplit(".", maxsplit=1)[-1]
    return operation in FORBIDDEN_ADAPTER_OPERATIONS and _is_adapter_binding(target)


@dataclass
class _BindingCollector:
    module_name: str
    scope: _LexicalScope
    violations: list[AdapterImportViolation]

    def record_non_demo_import(self, node: ast.AST, detail: str) -> None:
        if self.module_name == DEMO_MODULE:
            return
        _record_import_violation(self.module_name, node, detail, self.violations)

    def record_demo_import(
        self,
        node: ast.AST,
        imported_module: str,
        imported_name: str,
    ) -> None:
        if self.module_name != DEMO_MODULE:
            return
        if _demo_import_allowed(imported_module, imported_name):
            return
        _record_import_violation(
            self.module_name,
            node,
            (
                "demo adapter import must be operation-specific "
                f"({imported_module}.{imported_name})"
            ),
            self.violations,
        )

    def collect_from_alias(
        self,
        node: ast.AST,
        imported_module: str,
        alias: ast.alias,
    ) -> None:
        if alias.name == "*":
            self.record_non_demo_import(
                node,
                f"wildcard import from {imported_module}",
            )
            return
        local_name = alias.asname or alias.name
        _register_binding(
            self.scope,
            local_name,
            f"{imported_module}.{alias.name}",
        )
        self.record_demo_import(node, imported_module, alias.name)
        self.record_non_demo_import(
            node,
            f"imports {imported_module}.{alias.name}",
        )


def _process_import(
    module_name: str,
    node: ast.Import,
    scope: _LexicalScope,
    violations: list[AdapterImportViolation],
) -> None:
    for alias in node.names:
        if not _is_adapter_module(alias.name):
            continue
        local_name = alias.asname or alias.name.split(".")[-1]
        _register_binding(scope, local_name, alias.name)
        if module_name == DEMO_MODULE:
            _record_import_violation(
                module_name,
                node,
                f"demo may not import adapter package {alias.name!r}",
                violations,
            )
        else:
            _record_import_violation(
                module_name,
                node,
                f"imports adapter package {alias.name!r}",
                violations,
            )


def _process_import_from(
    module_name: str,
    node: ast.ImportFrom,
    scope: _LexicalScope,
    violations: list[AdapterImportViolation],
) -> None:
    if not _is_adapter_module(node.module):
        return
    imported_module = node.module or ""
    collector = _BindingCollector(module_name, scope, violations)
    for alias in node.names:
        collector.collect_from_alias(node, imported_module, alias)


def _record_forbidden_adapter_operation_call(
    module_name: str,
    node: ast.Call,
    operation: str,
    violations: list[AdapterImportViolation],
) -> None:
    _record_import_violation(
        module_name,
        node,
        f"calls forbidden adapter operation {operation}",
        violations,
    )


@dataclass(frozen=True, slots=True)
class _ForbiddenCallContext:
    module_name: str
    node: ast.Call
    scope: _LexicalScope
    violations: list[AdapterImportViolation]


def _root_name_forbidden_adapter_call(
    context: _ForbiddenCallContext,
    root_name: str,
    operation: str,
) -> bool:
    if root_name in ADAPTER_SUBMODULE_SHORT_NAMES:
        _record_forbidden_adapter_operation_call(
            context.module_name,
            context.node,
            f".{operation}()",
            context.violations,
        )
        return True
    target = context.scope.resolve(root_name)
    if target is not None and _is_adapter_binding(target):
        _record_forbidden_adapter_operation_call(
            context.module_name,
            context.node,
            f".{operation}()",
            context.violations,
        )
        return True
    return False


def _check_call(
    module_name: str,
    node: ast.Call,
    scope: _LexicalScope,
    violations: list[AdapterImportViolation],
) -> None:
    context = _ForbiddenCallContext(
        module_name=module_name,
        node=node,
        scope=scope,
        violations=violations,
    )
    func = node.func
    if isinstance(func, ast.Name):
        target = scope.resolve(func.id)
        if target is not None and _is_forbidden_adapter_operation_binding(target):
            _record_forbidden_adapter_operation_call(
                module_name,
                node,
                f"{target}()",
                violations,
            )
        return
    if not isinstance(func, ast.Attribute):
        return
    if func.attr not in FORBIDDEN_ADAPTER_OPERATIONS:
        return
    if isinstance(func.value, ast.Name):
        _root_name_forbidden_adapter_call(context, func.value.id, func.attr)
        return
    if isinstance(func.value, ast.Attribute) and isinstance(func.value.value, ast.Name):
        _root_name_forbidden_adapter_call(
            context,
            func.value.value.id,
            func.attr,
        )


def _scan_calls_in_same_scope(
    module_name: str,
    node: ast.AST,
    scope: _LexicalScope,
    violations: list[AdapterImportViolation],
) -> None:
    for child in ast.walk(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(child, ast.Call):
            _check_call(module_name, child, scope, violations)


def _scan_compound_statement(
    module_name: str,
    statement: ast.stmt,
    scope: _LexicalScope,
    violations: list[AdapterImportViolation],
) -> None:
    if isinstance(statement, ast.If):
        _scan_statements(module_name, statement.body, scope, violations)
        _scan_statements(module_name, statement.orelse, scope, violations)
        return
    if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
        _scan_statements(module_name, statement.body, scope, violations)
        _scan_statements(module_name, statement.orelse, scope, violations)
        return
    if isinstance(statement, ast.With):
        _scan_statements(module_name, statement.body, scope, violations)
        return
    if isinstance(statement, ast.Try):
        _scan_statements(module_name, statement.body, scope, violations)
        for handler in statement.handlers:
            _scan_statements(module_name, handler.body, scope, violations)
        _scan_statements(module_name, statement.orelse, scope, violations)
        _scan_statements(module_name, statement.finalbody, scope, violations)
        return
    if isinstance(statement, ast.Match):
        for match_case in statement.cases:
            _scan_statements(module_name, match_case.body, scope, violations)


def _scan_statements(
    module_name: str,
    statements: list[ast.stmt],
    scope: _LexicalScope,
    violations: list[AdapterImportViolation],
) -> None:
    for statement in statements:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _scan_statements(
                module_name,
                statement.body,
                _child_scope(scope),
                violations,
            )
            continue
        if isinstance(statement, ast.Import):
            _process_import(module_name, statement, scope, violations)
            continue
        if isinstance(statement, ast.ImportFrom):
            _process_import_from(module_name, statement, scope, violations)
            continue
        if isinstance(
            statement,
            (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.Try, ast.Match),
        ):
            _scan_compound_statement(module_name, statement, scope, violations)
            continue
        _scan_calls_in_same_scope(module_name, statement, scope, violations)


def scan_module_source(
    module_name: str,
    source: str,
) -> list[AdapterImportViolation]:
    tree = ast.parse(source)
    violations: list[AdapterImportViolation] = []
    _scan_statements(module_name, tree.body, _LexicalScope(), violations)
    return violations


def scan_module_path(
    module_name: str, module_path: Path
) -> list[AdapterImportViolation]:
    return scan_module_source(module_name, module_path.read_text(encoding="utf-8"))


def scan_all_non_gate_production_modules() -> dict[str, list[AdapterImportViolation]]:
    return {
        module_name: scan_module_path(module_name, module_path)
        for module_name, module_path in non_gate_production_modules()
    }


def _module_matches_package_root(module_name: str, package_root: str) -> bool:
    return module_name == package_root or module_name.startswith(f"{package_root}.")


def _parse_importlinter_contract_sections() -> dict[str, dict[str, str | list[str]]]:
    parser = configparser.ConfigParser()
    parser.read(IMPORTLINTER_CONFIG, encoding="utf-8")
    contracts: dict[str, dict[str, str | list[str]]] = {}
    for section in parser.sections():
        if not section.startswith("importlinter:contract:"):
            continue
        contract_id = section.removeprefix("importlinter:contract:")
        option_values: dict[str, str | list[str]] = {}
        for option in parser.options(section):
            if option in {"source_modules", "forbidden_modules", "layers"}:
                raw = parser.get(section, option)
                option_values[option] = [
                    line.strip()
                    for line in raw.splitlines()
                    if line.strip() and not line.strip().startswith("|")
                ]
            else:
                option_values[option] = parser.get(section, option)
        contracts[contract_id] = option_values
    return contracts


def importlinter_adapter_mediation_source_roots() -> list[str]:
    contract = _parse_importlinter_contract_sections()["adapter_mediation"]
    source_modules = contract.get("source_modules")
    assert isinstance(source_modules, list)
    return source_modules


def importlinter_adapter_mediation_forbidden_roots() -> list[str]:
    contract = _parse_importlinter_contract_sections()["adapter_mediation"]
    forbidden_modules = contract.get("forbidden_modules")
    assert isinstance(forbidden_modules, list)
    return forbidden_modules


def importlinter_adapter_mediation_has_source_forbidden_overlap() -> bool:
    """True when adapter_mediation source and forbidden packages overlap.

    In that configuration import-linter may keep some future adapter imports green
    even though the AST scanner must still reject them.
    """
    for source_root in importlinter_adapter_mediation_source_roots():
        for forbidden_root in importlinter_adapter_mediation_forbidden_roots():
            if (
                source_root == forbidden_root
                or source_root.startswith(f"{forbidden_root}.")
                or forbidden_root.startswith(f"{source_root}.")
            ):
                return True
    return False


def modules_covered_by_importlinter_adapter_mediation() -> set[str]:
    covered: set[str] = set()
    for module_name in discover_production_module_names():
        if any(
            _module_matches_package_root(module_name, root)
            for root in importlinter_adapter_mediation_source_roots()
        ):
            covered.add(module_name)
    return covered

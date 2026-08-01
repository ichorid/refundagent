"""Load-bearing architecture claims must match live code and config.

Mutation examples:
- add a ``CaseOutcome`` value without updating §6;
- add a façade keyword parameter without updating §9;
- add an ``.importlinter`` contract without updating §16's numbered list;
- restore a resolved-defect narrative in an invariant module docstring.
"""

from __future__ import annotations

import ast
import inspect
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin

import pytest

from saferefund.domain.enums import CaseOutcome
from saferefund.gate import (
    approve_refund,
    confirm_verification,
    escalate_case,
    execute_agent_action,
    expire_due_refunds_for_customer,
    reject_refund,
    send_unknown_sender_reply,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ARCHITECTURE_DOCUMENT = REPOSITORY_ROOT / "docs" / "ARCHITECTURE.md"
README_DOCUMENT = REPOSITORY_ROOT / "README.md"
DEBT_DOCUMENT = REPOSITORY_ROOT / "DEBT.md"
IMPORTLINTER_CONFIG = REPOSITORY_ROOT / ".importlinter"
INVARIANTS_DIRECTORY = REPOSITORY_ROOT / "tests" / "invariants"
LOAD_BEARING_DOCUMENTS: tuple[Path, ...] = (
    README_DOCUMENT,
    ARCHITECTURE_DOCUMENT,
    DEBT_DOCUMENT,
)

INVARIANT_MODULE_NARRATIVE_PATHS: tuple[Path, ...] = (
    INVARIANTS_DIRECTORY / "test_append_only_immutability.py",
    INVARIANTS_DIRECTORY / "test_approval_one_shot_concurrency.py",
    INVARIANTS_DIRECTORY / "test_model_boundary_untrusted_dependency.py",
    INVARIANTS_DIRECTORY / "test_refund_index_ddl_dialects.py",
)

OBSOLETE_RESOLVED_DEFECT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "missing append-only immutability guard",
        re.compile(
            r"no guard of any kind|unwritten convention|not merely intended",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "pre-lock approval decision",
        re.compile(
            r"reads and checks\s+`?refund\.status`?\s+\*?before\*?\s+it takes the "
            r"customer lock|both observe\s+`?pending_approval`?",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "missing model exception or timeout boundary",
        re.compile(
            r"has no exception boundary and no wall-clock bound|"
            r"leaves the case permanently\s+open|"
            r"defeats both hard limits",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "missing PostgreSQL partial-index DDL",
        re.compile(
            r"`sqlite_where`\s+is dialect-scoped[\s\S]{0,200}"
            r"unconditional unique index|"
            r"compiles to an\s+unconditional unique index",
            flags=re.IGNORECASE,
        ),
    ),
)

FACADE_OPERATIONS: dict[str, Callable[..., Any]] = {
    "execute_agent_action": execute_agent_action,
    "escalate_case": escalate_case,
    "expire_due_refunds_for_customer": expire_due_refunds_for_customer,
    "approve_refund": approve_refund,
    "reject_refund": reject_refund,
    "confirm_verification": confirm_verification,
    "send_unknown_sender_reply": send_unknown_sender_reply,
}

NUMBERED_CONTRACT_ITEM_PATTERN = re.compile(
    r"^\d+\.\s+\*\*`([^`]+)`\*\*\s+—",
    flags=re.MULTILINE,
)


def _architecture_text() -> str:
    return ARCHITECTURE_DOCUMENT.read_text(encoding="utf-8")


def _architecture_section_16(text: str) -> str:
    section_match = re.search(
        r"## 16\. Layout and Import Contracts\n(.*?)## 17\.",
        text,
        flags=re.DOTALL,
    )
    assert section_match is not None, "architecture §16 is missing"
    return section_match.group(1)


def _parse_documented_numbered_import_contracts(text: str) -> list[str]:
    section_text = _architecture_section_16(text)
    return NUMBERED_CONTRACT_ITEM_PATTERN.findall(section_text)


def _importlinter_contract_names_in_order() -> list[str]:
    return re.findall(
        r"\[importlinter:contract:([^\]]+)\]",
        IMPORTLINTER_CONFIG.read_text(encoding="utf-8"),
    )


def _normalize_type_name(annotation: object) -> str:  # noqa: PLR0911
    if annotation is inspect.Signature.empty:
        return "Any"
    if annotation is type(None):
        return "None"
    if isinstance(annotation, str):
        return annotation
    origin = get_origin(annotation)
    if origin in {Union, UnionType}:
        return " | ".join(_normalize_type_name(arg) for arg in get_args(annotation))
    if origin is None:
        if hasattr(annotation, "__name__"):
            return str(annotation.__name__)
        return str(annotation)
    args = get_args(annotation)
    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return f"tuple[{_normalize_type_name(args[0])}, ...]"
        inner = ", ".join(_normalize_type_name(arg) for arg in args)
        return f"tuple[{inner}]"
    origin_name = getattr(origin, "__name__", str(origin))
    if not args:
        return origin_name
    inner = ", ".join(_normalize_type_name(arg) for arg in args)
    return f"{origin_name}[{inner}]"


def _format_parameter(param: inspect.Parameter) -> str:
    prefix = ""
    if param.kind is inspect.Parameter.VAR_KEYWORD:
        prefix = "**"
    elif param.kind is inspect.Parameter.VAR_POSITIONAL:
        prefix = "*"

    annotation = _normalize_type_name(param.annotation)
    if param.default is inspect.Parameter.empty:
        return f"{prefix}{param.name}: {annotation}"
    if param.default is None:
        return f"{prefix}{param.name}: {annotation} = None"
    return f"{prefix}{param.name}: {annotation} = {param.default!r}"


def _format_live_signature(func: Callable[..., Any]) -> str:
    signature = inspect.signature(func)
    rendered: list[str] = []
    keyword_only_started = False
    for param in signature.parameters.values():
        if param.kind is inspect.Parameter.KEYWORD_ONLY and not keyword_only_started:
            rendered.append("*")
            keyword_only_started = True
        rendered.append(_format_parameter(param))
    return_annotation = _normalize_type_name(signature.return_annotation)
    return _normalize_signature_text(
        f"async def {func.__name__}({', '.join(rendered)}) -> {return_annotation}",
    )


def _normalize_signature_text(signature: str) -> str:
    collapsed = re.sub(r"\s+", " ", signature).replace("( ", "(").strip()
    return collapsed.replace(", )", ")")


def _parse_documented_facade_signatures(text: str) -> dict[str, str]:
    section_match = re.search(
        r"## 9\. Gate — the application boundary\n(.*?)### 9\.1 ",
        text,
        flags=re.DOTALL,
    )
    assert section_match is not None, "architecture §9 signature block is missing"
    code_block_match = re.search(
        r"```python\n(.*?)```",
        section_match.group(1),
        flags=re.DOTALL,
    )
    assert code_block_match is not None, "architecture §9 python block is missing"

    block = code_block_match.group(1)
    signatures: dict[str, str] = {}
    search_from = 0
    while True:
        start = block.find("async def ", search_from)
        if start == -1:
            break
        depth = 0
        end = start
        for position, character in enumerate(block[start:], start=start):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    end = position + 1
                    while end < len(block) and block[end] in " \t":
                        end += 1
                    if block.startswith("->", end):
                        arrow_end = block.find("\n", end)
                        end = len(block) if arrow_end == -1 else arrow_end
                    break
        signature = _normalize_signature_text(block[start:end])
        name_match = re.match(r"async def (\w+)\(", signature)
        assert name_match is not None, (
            f"could not parse documented signature: {signature}"
        )
        signatures[name_match.group(1)] = signature
        search_from = end
    return signatures


def _parse_documented_case_outcomes(text: str) -> set[str]:
    match = re.search(
        r"`case_closed\.outcome`\s*∈\s*(.+)",
        text,
    )
    assert match is not None, "architecture §6 case_closed.outcome set is missing"
    outcome_line = match.group(1).split("\n", maxsplit=1)[0]
    return set(re.findall(r"`([^`]+)`", outcome_line))


def test_documented_case_outcomes_match_the_enum() -> None:
    """Mutation: add ``CaseOutcome`` without updating §6's documented outcome set."""
    documented = _parse_documented_case_outcomes(_architecture_text())
    live = {member.value for member in CaseOutcome}
    assert documented == live


def test_documented_facade_signatures_match_the_code() -> None:
    """Mutation: add a keyword-only façade parameter without updating §9."""
    documented = _parse_documented_facade_signatures(_architecture_text())
    assert set(documented) == set(FACADE_OPERATIONS)

    mismatches: list[str] = []
    for name, func in FACADE_OPERATIONS.items():
        documented_signature = documented[name]
        live_signature = _format_live_signature(func)
        if documented_signature != live_signature:
            mismatches.append(
                f"{name}:\n  documented: {documented_signature}\n"
                f"  live:       {live_signature}",
            )
    assert mismatches == []


def _render_numbered_contract_section(contract_ids: list[str]) -> str:
    return "\n".join(
        f"{index}. **`{contract_id}`** — import-linter contract {index}."
        for index, contract_id in enumerate(contract_ids, start=1)
    )


def _assert_documented_import_contracts_match_live(
    documented_contracts: list[str],
) -> None:
    live_contracts = _importlinter_contract_names_in_order()
    assert documented_contracts == live_contracts


def test_documented_import_contracts_match_the_importlinter_file() -> None:
    """Mutation: add ``.importlinter`` contract without updating §16 numbered list."""
    documented_contracts = _parse_documented_numbered_import_contracts(
        _architecture_text(),
    )
    _assert_documented_import_contracts_match_live(documented_contracts)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda live: live[:-1],
        lambda live: [*live, "phantom_contract"],
        lambda live: [live[0], *live],
        lambda live: [*live[:-1], f"{live[-1]}_renamed"],
        lambda live: [live[0], live[2], live[1], *live[3:]],
    ],
    ids=["missing", "extra", "duplicate", "renamed", "reordered"],
)
def test_numbered_import_contract_parser_detects_documentation_drift(
    mutator: Callable[[list[str]], list[str]],
) -> None:
    """Mutation: drift in §16's numbered import-contract list must fail the guard."""
    live_contracts = _importlinter_contract_names_in_order()
    mutated_section = _render_numbered_contract_section(mutator(live_contracts))
    parsed_contracts = NUMBERED_CONTRACT_ITEM_PATTERN.findall(mutated_section)

    with pytest.raises(AssertionError):
        _assert_documented_import_contracts_match_live(parsed_contracts)


def _module_docstring(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return ast.get_docstring(tree) or ""


def _obsolete_resolved_defect_matches(docstring: str) -> list[str]:
    violations: list[str] = []
    for label, pattern in OBSOLETE_RESOLVED_DEFECT_PATTERNS:
        if pattern.search(docstring):
            violations.append(label)
    return violations


def _assert_invariant_module_docstrings_are_current(
    paths: Iterable[Path],
) -> None:
    violations: list[str] = []
    for path in paths:
        docstring = _module_docstring(path)
        matched_labels = _obsolete_resolved_defect_matches(docstring)
        if matched_labels:
            relative_path = path.relative_to(REPOSITORY_ROOT)
            violations.append(
                f"{relative_path}: obsolete resolved-defect narrative for "
                f"{', '.join(matched_labels)}",
            )
    assert violations == []


def test_invariant_module_narratives_do_not_assert_resolved_defects_are_current() -> (
    None
):
    """Mutation: restore a pre-fix defect claim in an invariant module docstring."""
    _assert_invariant_module_docstrings_are_current(INVARIANT_MODULE_NARRATIVE_PATHS)


@pytest.mark.parametrize(
    ("obsolete_docstring", "expected_label"),
    [
        (
            "There is no guard of any kind: callers rely on an unwritten convention.",
            "missing append-only immutability guard",
        ),
        (
            (
                "`approve_refund` reads and checks `refund.status` *before* it takes "
                "the customer lock."
            ),
            "pre-lock approval decision",
        ),
        (
            (
                "`await model.propose(prompt)` has no exception boundary and no "
                "wall-clock bound."
            ),
            "missing model exception or timeout boundary",
        ),
        (
            (
                "`sqlite_where` is dialect-scoped. On PostgreSQL the same `Index` "
                "compiles to an unconditional unique index on `order_id`."
            ),
            "missing PostgreSQL partial-index DDL",
        ),
    ],
)
def test_resolved_defect_narrative_scanner_detects_obsolete_statements(
    obsolete_docstring: str,
    expected_label: str,
) -> None:
    """Mutation: each known obsolete statement must fail the drift guard."""
    matched_labels = _obsolete_resolved_defect_matches(obsolete_docstring)
    assert expected_label in matched_labels


def _load_bearing_document_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in LOAD_BEARING_DOCUMENTS
    )


def _architecture_section_12_0(text: str) -> str:
    section_match = re.search(
        r"### 12\.0 Model gateway \(trusted boundary\)\n(.*?)(?:\n### |\Z)",
        text,
        flags=re.DOTALL,
    )
    assert section_match is not None, "architecture §12.0 is missing"
    return section_match.group(1)


def test_documented_order_disclosure_requires_orders_listed_event() -> None:
    """Mutation: claim pre-authorization order visibility without ``orders_listed``."""
    from saferefund.agent.prompt import disclosed_order_ids

    disclosure_source = inspect.getsource(disclosed_order_ids)
    assert "EventType.ORDERS_LISTED" in disclosure_source

    documentation = _load_bearing_document_text()
    assert "orders_listed" in documentation
    assert "disclosed_order_ids" in documentation


def test_documented_model_trust_boundary_matches_model_gateway() -> None:
    """Mutation: document a different trusted/untrusted split than ``ModelGateway``."""
    from saferefund.agent.gateway import ModelGateway

    architecture_section = _architecture_section_12_0(_architecture_text())
    readme_text = README_DOCUMENT.read_text(encoding="utf-8")

    for document_text in (architecture_section, readme_text):
        assert "ModelGateway" in document_text
        assert "Trusted:" in document_text or "trusted" in document_text.lower()
        assert "Untrusted:" in document_text or "untrusted" in document_text.lower()
        assert "response bytes" in document_text.lower()

    gateway_docstring = ModelGateway.__doc__ or ""
    assert "trusted" in gateway_docstring.lower()

    loop_signature = inspect.signature(
        __import__("saferefund.agent.loop", fromlist=["run_agent_loop"]).run_agent_loop,
    )
    gateway_parameter = loop_signature.parameters["model_gateway"]
    assert "ModelGateway" in _normalize_type_name(gateway_parameter.annotation)


def test_documented_model_boundary_owns_type_validation_and_parsing() -> None:
    """Mutation: move type checks or parsing outside ``invoke_model_boundary``."""
    from saferefund.agent import model_boundary

    documentation = _load_bearing_document_text()
    assert "invoke_model_boundary" in documentation

    boundary_source = inspect.getsource(model_boundary.invoke_model_boundary)
    assert "_require_exact_str" in boundary_source
    assert "parse(" in boundary_source
    assert "ModelGateway" in boundary_source


def test_documented_refund_intent_immutability_matches_enforcement() -> None:
    """Mutation: change immutable refund intent fields without updating the docs."""
    from saferefund.domain import tables as tables_module

    enforced_fields = tables_module._REFUND_INTENT_FIELD_NAMES  # noqa: SLF001
    documentation = _load_bearing_document_text().lower()
    missing_fields = [
        field_name
        for field_name in sorted(enforced_fields)
        if field_name not in documentation
    ]
    assert missing_fields == []
    assert "validate_refund_intent_against_proposed_evidence" in documentation


EXACT_SEQUENCE_SCENARIO_MODULES: dict[str, frozenset[str]] = {
    "tests/integration/test_approval_expiry.py": frozenset(
        {
            "assert_exact_event_type_sequence",
            "assert_case_expired_with_agent_resume",
        },
    ),
    "tests/integration/test_termination.py": frozenset(
        {"assert_terminal_escalation_closure"},
    ),
    "tests/integration/test_api_smoke.py": frozenset(
        {
            "assert_operator_approve_response_lifecycle",
            "assert_operator_reject_response_lifecycle",
            "assert_operator_conflict_response_unchanged",
        },
    ),
    "tests/integration/test_model_boundary.py": frozenset(
        {"assert_terminal_escalation_closure"},
    ),
}

DOCUMENTED_EXACT_SEQUENCE_SCENARIOS: tuple[str, ...] = (
    "test_approval_expires",
    "test_operator_approve_response_matches_exact_effect_and_event_sequence",
    "test_operator_reject_response_matches_exact_no_payment_sequence",
    "test_denial_loop_forces_escalation",
    "test_agent_escalation_closes_case",
    "test_step_limit",
    "test_parse_failure_limit",
    "test_non_string_model_output_escalates_and_closes_case",
)


def test_documented_exact_sequence_scenarios_use_sequence_helpers() -> None:
    """Mutation: cite an exact lifecycle scenario without the shared sequence helper."""
    documentation = _load_bearing_document_text()
    for scenario_name in DOCUMENTED_EXACT_SEQUENCE_SCENARIOS:
        assert scenario_name in documentation

    for module_path, required_helpers in EXACT_SEQUENCE_SCENARIO_MODULES.items():
        module_source = (REPOSITORY_ROOT / module_path).read_text(encoding="utf-8")
        missing_helpers = sorted(
            helper_name
            for helper_name in required_helpers
            if helper_name not in module_source
        )
        assert missing_helpers == [], (
            f"{module_path} must import/use {missing_helpers} for exact-sequence proof"
        )


def test_documented_future_module_adapter_scanning_is_exhaustive() -> None:
    """Mutation: claim import-linter alone covers future adapter imports."""
    from tests.invariants.adapter_import_scanner import (
        discover_production_module_names,
        scan_all_non_gate_production_modules,
    )

    documentation = _load_bearing_document_text()
    assert "adapter_import_scanner" in documentation
    assert (
        "test_every_non_gate_production_module_is_scanned_for_adapter_imports"
        in documentation
    )
    assert "layering hygiene" in documentation.lower()
    assert "import-linter" in documentation.lower()

    assert discover_production_module_names()
    assert isinstance(scan_all_non_gate_production_modules(), dict)

    for document_text in documentation.splitlines():
        if _claims_importlinter_is_security_boundary(document_text):
            pytest.fail(
                "import-linter must not be called a security boundary "
                "in load-bearing docs",
            )


def _claims_importlinter_is_security_boundary(line: str) -> bool:
    lowered = line.lower()
    if "import-linter" not in lowered or "security boundary" not in lowered:
        return False
    if re.search(
        r"\b(?:not|neither)\b[^.\n]{0,80}\bsecurity boundary\b",
        lowered,
    ):
        return False
    return (
        re.search(
            r"\bsecurity boundary\b[^.\n]{0,20}\b(?:not|neither)\b",
            lowered,
        )
        is None
    )


def test_documented_relational_scope_checks_exist() -> None:
    """Mutation: drop relational scope enforcement from the documented guarantees."""
    from saferefund.repositories.relational_scope import (
        InvalidEventRelationalScopeError,
        validate_event_relational_scope,
    )

    documentation = _load_bearing_document_text()
    assert "relational_scope" in documentation
    assert "test_event_append_rejects_foreign_case_or_order" in documentation
    assert inspect.isfunction(validate_event_relational_scope)
    assert issubclass(InvalidEventRelationalScopeError, Exception)


def test_documented_postgresql_concurrency_points_to_postgres_tests() -> None:
    """Mutation: claim PG contention without naming ``tests/postgres`` evidence."""
    from tests.postgres.conftest import TESTED_POSTGRESQL_VERSION

    documentation = _load_bearing_document_text()
    assert "tests/postgres" in documentation
    assert TESTED_POSTGRESQL_VERSION in documentation
    assert "test_refund_threshold_concurrency" in documentation
    assert "test_event_sequence_concurrency" in documentation
    assert "test_operator_concurrency" in documentation

    stale_claim = re.compile(
        r"no test (?:in this repository )?exercises PostgreSQL contention",
        flags=re.IGNORECASE,
    )
    assert stale_claim.search(documentation) is None


ABSOLUTE_EVENT_CORRESPONDENCE_PATTERN = re.compile(
    r"every transition has a (?:matching )?event",
    flags=re.IGNORECASE,
)
ATOMIC_EXTERNAL_EFFECT_PATTERN = re.compile(
    r"\batomic(?:ally)?\b",
    flags=re.IGNORECASE,
)
EXTERNAL_EFFECT_TERMS_PATTERN = re.compile(
    r"\b(?:mailer|ticketing|reply_sent|verification token|external effect)\b",
    flags=re.IGNORECASE,
)
CRASH_WINDOW_OR_OUTBOX_PATTERN = re.compile(
    r"\b(?:crash window|transactional outbox|effect-first)\b",
    flags=re.IGNORECASE,
)


def _external_effect_claim_violations(document_text: str, label: str) -> list[str]:
    violations: list[str] = []
    if ABSOLUTE_EVENT_CORRESPONDENCE_PATTERN.search(document_text):
        violations.append(
            f"{label}: absolute event/effect correspondence claim "
            "without qualification",
        )

    for match in ATOMIC_EXTERNAL_EFFECT_PATTERN.finditer(document_text):
        window_start = max(0, match.start() - 240)
        window_end = min(len(document_text), match.end() + 240)
        context = document_text[window_start:window_end]
        if re.search(r"\bnot\b.{0,40}\batomic", context, flags=re.IGNORECASE):
            continue
        if EXTERNAL_EFFECT_TERMS_PATTERN.search(context) and (
            CRASH_WINDOW_OR_OUTBOX_PATTERN.search(context) is None
        ):
            violations.append(
                f"{label}: atomic external-effect claim without crash window/outbox "
                f"near offset {match.start()}",
            )
    return violations


def test_external_effect_claim_names_crash_windows_or_outbox_guarantee() -> None:
    """Mutation: claim atomic external effects without naming the crash window."""
    violations: list[str] = []
    for document_path in LOAD_BEARING_DOCUMENTS:
        violations.extend(
            _external_effect_claim_violations(
                document_path.read_text(encoding="utf-8"),
                document_path.name,
            ),
        )
    assert violations == []

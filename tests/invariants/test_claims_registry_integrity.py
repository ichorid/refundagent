"""`docs/CLAIMS.md` pointers must stay live.

This does not check that a claim is *true* — the underlying guard test (for
``enforced`` rows) or a human reading ``DEBT.md`` (for ``convention`` rows) does that.
It only checks that the pointer resolves: a renamed/deleted guard test, or a rewritten
``DEBT.md`` bullet, must turn this suite red immediately rather than going stale until
the next manual claims audit.

Mutation examples:
- rename ``test_documented_order_disclosure_requires_orders_listed_event`` without
  updating CLM-001's guard-test column;
- reword a ``DEBT.md`` bullet without updating the matching ``convention`` row's quote;
- add an ``enforced`` row whose guard test does not exist.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CLAIMS_DOCUMENT = REPOSITORY_ROOT / "docs" / "CLAIMS.md"
DEBT_DOCUMENT = REPOSITORY_ROOT / "DEBT.md"

ENFORCED_ROW_PATTERN = re.compile(
    r"^\| (CLM-\d+) \| (.+?) \| (.+?) \| "
    r"`([^`]+\.py)::([A-Za-z_][A-Za-z0-9_]*)` \| (enforced[^|]*) \|$",
    flags=re.MULTILINE,
)
CONVENTION_ROW_PATTERN = re.compile(
    r"^\| (CLM-\d+) \| (.+?) \| `([^`]+)` \| (convention) \|$",
    flags=re.MULTILINE,
)


def _claims_text() -> str:
    return CLAIMS_DOCUMENT.read_text(encoding="utf-8")


def _enforced_rows() -> list[tuple[str, str, str]]:
    """Return ``(claim_id, module_relative_path, function_name)`` for enforced rows."""
    rows = ENFORCED_ROW_PATTERN.findall(_claims_text())
    return [
        (claim_id, module_path, function_name)
        for claim_id, _claim, _mechanism, module_path, function_name, _boundary in rows
    ]


def _convention_rows() -> list[tuple[str, str]]:
    """Return ``(claim_id, debt_quote)`` for convention rows."""
    return [
        (claim_id, quote)
        for claim_id, _claim, quote, _boundary in CONVENTION_ROW_PATTERN.findall(
            _claims_text(),
        )
    ]


def _module_defines_function(module_relative_path: str, function_name: str) -> bool:
    module_path = REPOSITORY_ROOT / module_relative_path
    if not module_path.is_file():
        return False
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == function_name
        for node in ast.walk(tree)
    )


def test_registry_is_not_empty() -> None:
    """Mutation: delete every row from ``docs/CLAIMS.md`` while keeping the headers."""
    assert len(_enforced_rows()) >= 10
    assert len(_convention_rows()) >= 10


def test_every_enforced_claim_has_a_live_guard_test() -> None:
    """Mutation: rename/delete a guard test without updating its CLM row."""
    missing: list[str] = []
    for claim_id, module_path, function_name in _enforced_rows():
        if not _module_defines_function(module_path, function_name):
            missing.append(f"{claim_id}: {module_path}::{function_name} not found")
    assert missing == []


def test_every_convention_claim_has_a_matching_debt_bullet() -> None:
    """Mutation: reword a ``DEBT.md`` bullet without updating the matching CLM quote."""
    debt_text = DEBT_DOCUMENT.read_text(encoding="utf-8")
    missing: list[str] = []
    for claim_id, quote in _convention_rows():
        if quote not in debt_text:
            missing.append(
                f"{claim_id}: quote not found verbatim in DEBT.md: {quote!r}"
            )
    assert missing == []


def test_claim_ids_are_unique() -> None:
    """Mutation: copy-paste a row without renumbering its CLM id."""
    all_ids = [claim_id for claim_id, *_ in _enforced_rows()] + [
        claim_id for claim_id, *_ in _convention_rows()
    ]
    duplicates = sorted(
        {claim_id for claim_id in all_ids if all_ids.count(claim_id) > 1}
    )
    assert duplicates == []


def test_registry_row_parser_detects_a_dangling_guard_test() -> None:
    """Mutation guard for the guard: a fabricated row's test must not resolve."""
    assert not _module_defines_function(
        "tests/invariants/test_claims_registry_integrity.py",
        "this_function_does_not_exist",
    )

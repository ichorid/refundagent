"""Atomic per-customer event sequence allocation and concurrency proofs."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.dialects.sqlite.base import SQLiteDialect
from sqlalchemy.schema import CreateTable
from sqlalchemy.sql.schema import Table

from saferefund.domain.tables import EventSequenceRow

if TYPE_CHECKING:
    from sqlalchemy.engine.interfaces import Dialect

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
EVENTS_REPOSITORY_PATH = SOURCE_ROOT / "saferefund" / "repositories" / "events.py"


def _postgresql_test_dialect() -> Dialect:
    dialect_type: type[Dialect] = cast("type[Dialect]", PGDialect)
    return dialect_type()


TARGET_DIALECTS: tuple[tuple[str, Dialect], ...] = (
    ("sqlite", SQLiteDialect()),
    ("postgresql", _postgresql_test_dialect()),
)


def _event_sequences_table() -> Table:
    table = EventSequenceRow.__table__
    assert isinstance(table, Table)
    return table


def _event_sequences_table_ddl(dialect: Dialect) -> str:
    return str(CreateTable(_event_sequences_table()).compile(dialect=dialect))


@pytest.mark.parametrize(("dialect_name", "dialect"), TARGET_DIALECTS)
def test_event_sequences_table_compiles_on_every_target_dialect(
    dialect_name: str,
    dialect: Dialect,
) -> None:
    """Mutation: drop ``event_sequences`` from ``tables.py``; DDL no longer compiles."""
    compiled_ddl = _event_sequences_table_ddl(dialect)

    assert "event_sequences" in compiled_ddl
    assert "customer_id" in compiled_ddl
    assert "next_seq" in compiled_ddl
    assert "NOT NULL" in compiled_ddl.upper(), (
        f"{dialect_name} must compile a non-null next_seq column: {compiled_ddl}"
    )
    assert "PRIMARY KEY" in compiled_ddl.upper() or "primary key" in compiled_ddl, (
        f"{dialect_name} must compile customer_id as the primary key: {compiled_ddl}"
    )
    assert "customers" in compiled_ddl, (
        f"{dialect_name} must compile a foreign key to customers: {compiled_ddl}"
    )


def _max_event_seq_query_sites(module_path: Path) -> list[str]:
    """Return production sites that query MAX(events.seq) for allocation."""
    module_tree = ast.parse(module_path.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(module_tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "max"
            and isinstance(func.value, ast.Name)
            and func.value.id == "func"
        ):
            continue
        if not node.args:
            continue
        argument = node.args[0]
        if (
            isinstance(argument, ast.Attribute)
            and argument.attr == "seq"
            and isinstance(argument.value, ast.Name)
            and argument.value.id == "EventRow"
        ):
            offenders.append(f"{module_path.relative_to(SOURCE_ROOT)}:{node.lineno}")
    return offenders


def test_production_event_allocation_does_not_query_max_event_seq() -> None:
    """Mutation: restore SELECT MAX(events.seq)+1 in events.py; this fails."""
    production_root = SOURCE_ROOT / "saferefund"
    offenders: list[str] = []
    for module_path in production_root.rglob("*.py"):
        offenders.extend(_max_event_seq_query_sites(module_path))

    assert offenders == [], (
        "event seq allocation must use the atomic event_sequences counter, not "
        f"MAX(EventRow.seq); offenders: {sorted(offenders)}"
    )

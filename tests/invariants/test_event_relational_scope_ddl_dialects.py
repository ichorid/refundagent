"""Composite ownership uniqueness and foreign keys must exist on every dialect."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest
from sqlalchemy import ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.dialects.sqlite.base import SQLiteDialect
from sqlalchemy.schema import CreateTable
from sqlalchemy.sql.schema import Table

from saferefund.domain.tables import CaseRow, EventRow, OrderRow, RefundRow

if TYPE_CHECKING:
    from sqlalchemy.engine.interfaces import Dialect


def _postgresql_test_dialect() -> Dialect:
    dialect_type: type[Dialect] = cast("type[Dialect]", PGDialect)
    return dialect_type()


TARGET_DIALECTS: tuple[tuple[str, Dialect], ...] = (
    ("sqlite", SQLiteDialect()),
    ("postgresql", _postgresql_test_dialect()),
)


def _orm_table(model: type[Any]) -> Table:
    table = model.__table__
    assert isinstance(table, Table)
    return table


def _unique_constraint_columns(table: Table, name: str) -> tuple[str, ...]:
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint) and constraint.name == name:
            return tuple(column.name for column in constraint.columns)
    message = f"{table.name} is missing unique constraint {name}"
    raise AssertionError(message)


def _foreign_key_constraint(
    table: Table,
    name: str,
) -> ForeignKeyConstraint:
    for constraint in table.constraints:
        if isinstance(constraint, ForeignKeyConstraint) and constraint.name == name:
            return constraint
    message = f"{table.name} is missing foreign key constraint {name}"
    raise AssertionError(message)


def _foreign_key_endpoints(
    constraint: ForeignKeyConstraint,
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    local_columns = tuple(column.name for column in constraint.columns)
    remote_columns = tuple(
        (element.column.table.name, element.column.name)
        for element in constraint.elements
    )
    return local_columns, remote_columns


@pytest.mark.parametrize(("dialect_name", "dialect"), TARGET_DIALECTS)
def test_case_and_order_composite_ownership_uniqueness_exists(
    dialect_name: str,
    dialect: Dialect,
) -> None:
    """Cases and orders expose composite ownership keys for relational scope."""
    del dialect_name
    assert _unique_constraint_columns(
        _orm_table(CaseRow), "uq_case_id_customer_id"
    ) == (
        "id",
        "customer_id",
    )
    assert _unique_constraint_columns(
        _orm_table(OrderRow), "uq_order_id_customer_id"
    ) == (
        "id",
        "customer_id",
    )

    case_ddl = str(CreateTable(_orm_table(CaseRow)).compile(dialect=dialect))
    order_ddl = str(CreateTable(_orm_table(OrderRow)).compile(dialect=dialect))
    assert "uq_case_id_customer_id" in case_ddl
    assert "uq_order_id_customer_id" in order_ddl


@pytest.mark.parametrize(("dialect_name", "dialect"), TARGET_DIALECTS)
def test_event_composite_foreign_keys_tie_scope_to_customer(
    dialect_name: str,
    dialect: Dialect,
) -> None:
    """Event rows must reference case and order ownership, not bare identifiers."""
    del dialect_name
    case_fk = _foreign_key_constraint(_orm_table(EventRow), "fk_events_case_customer")
    order_fk = _foreign_key_constraint(_orm_table(EventRow), "fk_events_order_customer")

    assert _foreign_key_endpoints(case_fk) == (
        ("case_id", "customer_id"),
        (("cases", "id"), ("cases", "customer_id")),
    )
    assert _foreign_key_endpoints(order_fk) == (
        ("order_id", "customer_id"),
        (("orders", "id"), ("orders", "customer_id")),
    )

    events_ddl = str(CreateTable(_orm_table(EventRow)).compile(dialect=dialect))
    assert "fk_events_case_customer" in events_ddl
    assert "fk_events_order_customer" in events_ddl


@pytest.mark.parametrize(("dialect_name", "dialect"), TARGET_DIALECTS)
def test_refund_composite_foreign_keys_bind_case_and_order_to_customer(
    dialect_name: str,
    dialect: Dialect,
) -> None:
    """Refund enforcement rows must agree with case and order ownership keys."""
    del dialect_name
    case_fk = _foreign_key_constraint(_orm_table(RefundRow), "fk_refunds_case_customer")
    order_fk = _foreign_key_constraint(
        _orm_table(RefundRow), "fk_refunds_order_customer"
    )

    assert _foreign_key_endpoints(case_fk) == (
        ("case_id", "customer_id"),
        (("cases", "id"), ("cases", "customer_id")),
    )
    assert _foreign_key_endpoints(order_fk) == (
        ("order_id", "customer_id"),
        (("orders", "id"), ("orders", "customer_id")),
    )

    refunds_ddl = str(CreateTable(_orm_table(RefundRow)).compile(dialect=dialect))
    assert "fk_refunds_case_customer" in refunds_ddl
    assert "fk_refunds_order_customer" in refunds_ddl

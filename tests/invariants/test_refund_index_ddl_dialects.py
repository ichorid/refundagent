"""The open-refund uniqueness index is partial on every supported dialect.

``uq_open_refund_per_order`` restricts uniqueness to live refund statuses on both
SQLite and PostgreSQL so historical refunds do not block new proposals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.dialects.sqlite.base import SQLiteDialect
from sqlalchemy.schema import CreateIndex
from sqlalchemy.sql.schema import Index, Table

from saferefund.domain.enums import RefundStatus
from saferefund.domain.tables import RefundRow

if TYPE_CHECKING:
    from sqlalchemy.engine.interfaces import Dialect

LIVE_REFUND_STATUS_VALUES = (
    RefundStatus.PENDING_APPROVAL.value,
    RefundStatus.APPROVED.value,
)


def _postgresql_test_dialect() -> Dialect:
    dialect_type: type[Dialect] = cast("type[Dialect]", PGDialect)
    return dialect_type()


TARGET_DIALECTS: tuple[tuple[str, Dialect], ...] = (
    ("sqlite", SQLiteDialect()),
    ("postgresql", _postgresql_test_dialect()),
)


def _refund_table() -> Table:
    table = RefundRow.__table__
    assert isinstance(table, Table)
    return table


def _open_refund_index() -> Index:
    for index in _refund_table().indexes:
        if index.name == "uq_open_refund_per_order":
            return index
    message = "uq_open_refund_per_order index is missing from the refunds table"
    raise AssertionError(message)


@pytest.mark.parametrize(("dialect_name", "dialect"), TARGET_DIALECTS)
def test_open_refund_index_is_partial_on_every_target_dialect(
    dialect_name: str,
    dialect: Dialect,
) -> None:
    """The uniqueness must be restricted to live refunds, not to every refund row."""
    compiled_ddl = str(CreateIndex(_open_refund_index()).compile(dialect=dialect))

    assert "UNIQUE INDEX" in compiled_ddl
    assert "WHERE" in compiled_ddl, (
        f"{dialect_name} compiles an unconditional unique index on order_id, "
        f"which permits only one refund row per order for all time: {compiled_ddl}"
    )
    for status_value in LIVE_REFUND_STATUS_VALUES:
        assert status_value in compiled_ddl

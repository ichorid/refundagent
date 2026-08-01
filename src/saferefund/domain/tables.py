"""SQLAlchemy tables for immutable seeds, canonical events, and live refunds."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    column,
    event,
)
from sqlalchemy import Enum as SqlEnum
from sqlalchemy import (
    inspect as sa_inspect,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Mapper, mapped_column, relationship

from saferefund.domain.enums import Actor, Channel, OrderStatus, RefundStatus


class Base(DeclarativeBase):
    """Base for all database tables in this application."""


class ImmutableRowError(RuntimeError):
    """Raised when a write is attempted against an append-only or seed table."""


class RefundIntentImmutableError(RuntimeError):
    """Raised when a refund enforcement row's authorization intent is mutated."""


class RefundStatusTransitionError(RuntimeError):
    """Raised when a refund status transition is not permitted."""


def _stored_enum(
    enum_type: type[Actor] | type[Channel] | type[OrderStatus] | type[RefundStatus],
) -> SqlEnum:
    return SqlEnum(
        enum_type,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda enum_members: [member.value for member in enum_members],
    )


class CustomerRow(Base):
    """Immutable customer seed row."""

    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)

    orders: Mapped[list["OrderRow"]] = relationship(back_populates="customer")
    cases: Mapped[list["CaseRow"]] = relationship(back_populates="customer")


class OrderRow(Base):
    """Immutable order seed row."""

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("id", "customer_id", name="uq_order_id_customer_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False)
    item: Mapped[str] = mapped_column(String, nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        _stored_enum(OrderStatus), nullable=False
    )

    customer: Mapped[CustomerRow] = relationship(back_populates="orders")


class CaseRow(Base):
    """Correlation row for one opening email."""

    __tablename__ = "cases"
    __table_args__ = (
        UniqueConstraint(
            "customer_id",
            "opening_message_id",
            name="uq_case_customer_opening_message_id",
        ),
        UniqueConstraint("id", "customer_id", name="uq_case_id_customer_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False)
    opening_message_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    customer: Mapped[CustomerRow] = relationship(back_populates="cases")


class EventSequenceRow(Base):
    """Per-customer atomic counter for event ``seq`` allocation.

    ``next_seq`` is the last allocated sequence number for the customer,
    starting at ``0`` before any event exists. Allocation atomically increments
    and returns the new value (the first event therefore receives ``seq=1``).
    """

    __tablename__ = "event_sequences"

    customer_id: Mapped[str] = mapped_column(
        ForeignKey("customers.id"), primary_key=True
    )
    next_seq: Mapped[int] = mapped_column(nullable=False)


class EventRow(Base):
    """Append-only canonical event evidence."""

    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("customer_id", "seq", name="uq_event_customer_seq"),
        ForeignKeyConstraint(
            ["case_id", "customer_id"],
            ["cases.id", "cases.customer_id"],
            name="fk_events_case_customer",
        ),
        ForeignKeyConstraint(
            ["order_id", "customer_id"],
            ["orders.id", "orders.customer_id"],
            name="fk_events_order_customer",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False)
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id"))
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"))
    seq: Mapped[int] = mapped_column(nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    actor: Mapped[Actor] = mapped_column(_stored_enum(Actor), nullable=False)
    channel: Mapped[Channel] = mapped_column(_stored_enum(Channel), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


_LIVE_REFUND_STATUS_VALUES = (
    RefundStatus.PENDING_APPROVAL.value,
    RefundStatus.APPROVED.value,
)
_live_refund_predicate = column("status").in_(_LIVE_REFUND_STATUS_VALUES)


class RefundRow(Base):
    """Mutable enforcement surface for the single-open-refund invariant."""

    __tablename__ = "refunds"
    __table_args__ = (
        Index(
            "uq_open_refund_per_order",
            "order_id",
            unique=True,
            sqlite_where=_live_refund_predicate,
            postgresql_where=_live_refund_predicate,
        ),
        ForeignKeyConstraint(
            ["case_id", "customer_id"],
            ["cases.id", "cases.customer_id"],
            name="fk_refunds_case_customer",
        ),
        ForeignKeyConstraint(
            ["order_id", "customer_id"],
            ["orders.id", "orders.customer_id"],
            name="fk_refunds_order_customer",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), nullable=False)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[RefundStatus] = mapped_column(
        _stored_enum(RefundStatus), nullable=False
    )
    approval_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


def _reject_immutable_row_update(
    mapper: Mapper[Any],
    _connection: object,
    _target: object,
) -> None:
    table_name = mapper.class_.__tablename__
    message = f"updates are not permitted on {table_name}"
    raise ImmutableRowError(message)


def _reject_immutable_row_delete(
    mapper: Mapper[Any],
    _connection: object,
    _target: object,
) -> None:
    table_name = mapper.class_.__tablename__
    message = f"deletes are not permitted on {table_name}"
    raise ImmutableRowError(message)


_REFUND_INTENT_FIELD_NAMES = frozenset(
    {"id", "customer_id", "order_id", "case_id", "amount", "created_at"},
)


def _reject_refund_intent_mutation(
    _mapper: Mapper[Any],
    _connection: object,
    target: object,
) -> None:
    """Permit only documented lifecycle mutations on refund enforcement rows."""
    if not isinstance(target, RefundRow):
        return
    instance_state = sa_inspect(target)
    for field_name in _REFUND_INTENT_FIELD_NAMES:
        attribute_state = instance_state.attrs[field_name]
        if attribute_state.history.has_changes():
            message = f"refund authorization intent field {field_name} is immutable"
            raise RefundIntentImmutableError(message)
    status_state = instance_state.attrs["status"]
    if status_state.history.has_changes():
        message = "refund status may only change via guarded SQL lifecycle transitions"
        raise RefundStatusTransitionError(message)


for _immutable_row_type in (CustomerRow, OrderRow, EventRow):
    event.listen(_immutable_row_type, "before_update", _reject_immutable_row_update)
    event.listen(_immutable_row_type, "before_delete", _reject_immutable_row_delete)

event.listen(RefundRow, "before_update", _reject_refund_intent_mutation)

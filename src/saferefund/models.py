"""Synchronous SQLAlchemy rows for the toy application."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    column,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base for all database tables in this application."""


class CaseStatus(StrEnum):
    """Lifecycle state of a support case."""

    OPEN = "open"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_VERIFICATION = "awaiting_verification"
    CLOSED = "closed"


class CaseOutcome(StrEnum):
    """Terminal outcome recorded when a case closes."""

    FINISHED = "finished"
    ESCALATED = "escalated"
    STEP_LIMIT = "step_limit"
    PARSE_LIMIT = "parse_limit"
    MODEL_FAILURE = "model_failure"


class RefundStatus(StrEnum):
    """Lifecycle state of a refund row."""

    PENDING_APPROVAL = "pending_approval"
    EXECUTED = "executed"
    REJECTED = "rejected"
    EXPIRED = "expired"


class OrderStatus(StrEnum):
    """Advisory seed status shown to the model."""

    DELIVERED = "delivered"
    DELIVERED_DAMAGED = "delivered_damaged"
    SHIPPED = "shipped"


class Customer(Base):
    """Customer account row."""

    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    verified: Mapped[bool] = mapped_column(nullable=False, default=False)


class Order(Base):
    """Customer order row."""

    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False)
    item: Mapped[str] = mapped_column(String, nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    refunded_total: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0")
    )


class Case(Base):
    """Correlation row for one opening email."""

    __tablename__ = "cases"
    __table_args__ = (
        UniqueConstraint(
            "customer_id",
            "opening_message_id",
            name="uq_case_customer_opening_message_id",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False)
    opening_message_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    outcome: Mapped[str | None] = mapped_column(String)
    linked_order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"))
    orders_listed: Mapped[bool] = mapped_column(nullable=False, default=False)
    refund_reply_sent: Mapped[bool] = mapped_column(nullable=False, default=False)
    step_count: Mapped[int] = mapped_column(nullable=False, default=0)
    consecutive_denials: Mapped[int] = mapped_column(nullable=False, default=0)
    consecutive_invalid_outputs: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class Refund(Base):
    """Mutable refund row.

    Policy rule 8 denies a second live refund first; the partial unique index below
    is the database backstop if the gate ever skips that check.
    """

    __tablename__ = "refunds"
    __table_args__ = (
        Index(
            "uq_open_refund_per_order",
            "order_id",
            unique=True,
            sqlite_where=column("status") == RefundStatus.PENDING_APPROVAL.value,
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), nullable=False)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    approval_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
    )
    provider_ref: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class VerificationToken(Base):
    """Out-of-band verification token."""

    __tablename__ = "verification_tokens"

    token: Mapped[str] = mapped_column(String, primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


class AuditEvent(Base):
    """Informational audit trail row."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    case_id: Mapped[str | None] = mapped_column(String)
    customer_id: Mapped[str | None] = mapped_column(String)
    type: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


@dataclass(frozen=True, slots=True)
class InboundResult:
    """Outcome of routing one inbound email."""

    case_id: str | None
    status: str
    unknown_sender: bool = False
    reopened_case_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OperatorResult:
    """Outcome of an operator approve or reject action."""

    case_id: str
    refund_id: str
    refund_status: str
    conflict: bool = False
    reopened_case_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Outcome of confirming a verification token."""

    found: bool
    expired: bool = False
    customer_id: str | None = None
    issuing_case_id: str | None = None
    open_case_ids: tuple[str, ...] = ()

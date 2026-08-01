"""Shared seed and event views for pure projection folds."""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from saferefund.domain.enums import Actor
from saferefund.domain.events import EventType


class FoldableEvent(Protocol):
    """Minimal event surface required to fold control-state summaries."""

    @property
    def customer_id(self) -> str: ...  # noqa: D102

    @property
    def case_id(self) -> str | None: ...  # noqa: D102

    @property
    def order_id(self) -> str | None: ...  # noqa: D102

    @property
    def seq(self) -> int: ...  # noqa: D102

    @property
    def event_type(self) -> EventType: ...  # noqa: D102

    @property
    def actor(self) -> Actor: ...  # noqa: D102

    @property
    def payload(self) -> Mapping[str, Any]: ...  # noqa: D102


@dataclass(frozen=True, slots=True)
class CustomerSeed:
    """Immutable customer row fields used by projections."""

    customer_id: str
    email: str


@dataclass(frozen=True, slots=True)
class OrderSeed:
    """Immutable order row fields used by projections."""

    order_id: str
    customer_id: str
    total: Decimal

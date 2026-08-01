"""Remove dead surface, make the heuristic model total, and stop the drift.

Three small things that together make the artifact describe itself inaccurately:
unused production abstractions nobody calls, a "deterministic" model stub that
raises on states its own declared type permits, and documented counts that do
not match the code.
"""

from __future__ import annotations

import ast
import re
from decimal import Decimal
from pathlib import Path

import pytest

from saferefund.agent.models import HeuristicModel, heuristic_action_json
from saferefund.agent.parsing import ParseSuccess, parse
from saferefund.agent.prompt import AgentState, OrderView, Prompt, UntrustedField
from saferefund.domain.enums import OrderStatus

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
GATE_SOURCE_DIRECTORY = REPOSITORY_ROOT / "src" / "saferefund" / "gate"
ARCHITECTURE_DOCUMENT = REPOSITORY_ROOT / "docs" / "ARCHITECTURE.md"

CALL_SITE_COUNT_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
}


def _agent_state(
    *,
    orders: tuple[OrderView, ...],
    linked_order_id: str | None,
) -> AgentState:
    return AgentState(
        verified=True,
        orders=orders,
        orders_listed=True,
        linked_order_id=linked_order_id,
        last_refund_status=None,
        reply_sent_after_last_refund=False,
        menu=("get_orders", "escalate", "finish"),
    )


def _order_view(order_id: str) -> OrderView:
    return OrderView(
        id=order_id,
        item=UntrustedField(provenance="untrusted", value="Espresso machine"),
        total=Decimal("249.00"),
        status=OrderStatus.DELIVERED_DAMAGED,
    )


@pytest.mark.parametrize(
    ("orders", "linked_order_id"),
    [
        ((), None),
        ((_order_view("ORD-1001"),), "ORD-9999"),
    ],
    ids=["no-orders", "linked-order-absent-from-state"],
)
def test_heuristic_model_is_total_over_its_declared_state(
    orders: tuple[OrderView, ...],
    linked_order_id: str | None,
) -> None:
    """Mutation: raise in heuristic_action_json when linked_order_id is absent."""
    state = _agent_state(orders=orders, linked_order_id=linked_order_id)

    raw_model_output = heuristic_action_json(state)

    assert isinstance(parse(raw_model_output), ParseSuccess)


async def test_heuristic_model_propose_is_total_over_its_declared_state() -> None:
    """Mutation: make ``HeuristicModel.propose`` raise on empty orders."""
    state = _agent_state(orders=(), linked_order_id=None)
    prompt = Prompt(text="", state=state)

    raw_model_output = await HeuristicModel().propose(prompt)

    assert isinstance(parse(raw_model_output), ParseSuccess)


def test_no_unused_production_abstractions_remain() -> None:
    """Mutation: re-export ``session_scope`` from ``saferefund.db``."""
    from saferefund import db
    from saferefund.api import dependencies

    dead_surface = {
        "saferefund.db.session_scope": hasattr(db, "session_scope"),
        "saferefund.db.sqlite_url": hasattr(db, "sqlite_url"),
        "saferefund.api.dependencies.get_db_session": hasattr(
            dependencies,
            "get_db_session",
        ),
        "saferefund.api.dependencies.DbSessionDep": hasattr(
            dependencies,
            "DbSessionDep",
        ),
    }
    assert {name for name, present in dead_surface.items() if present} == set()


def _expire_due_refunds_for_customer_call_sites() -> list[str]:
    call_sites: list[str] = []
    scan_roots = (
        REPOSITORY_ROOT / "src" / "saferefund" / "gate",
        REPOSITORY_ROOT / "src" / "saferefund" / "agent",
        REPOSITORY_ROOT / "src" / "saferefund" / "api",
    )
    for scan_root in scan_roots:
        for module_path in sorted(scan_root.rglob("*.py")):
            module_tree = ast.parse(module_path.read_text(encoding="utf-8"))
            relative_path = module_path.relative_to(
                REPOSITORY_ROOT / "src" / "saferefund"
            )
            for node in ast.walk(module_tree):
                if not isinstance(node, ast.Call):
                    continue
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "expire_due_refunds_for_customer"
                ) or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "expire_due_refunds_for_customer"
                ):
                    call_sites.append(f"{relative_path}:{node.lineno}")
    return call_sites


def test_documented_expiry_call_site_count_matches_the_code() -> None:
    """Mutation: add an expiry call site without updating the architecture count."""
    architecture_text = ARCHITECTURE_DOCUMENT.read_text(encoding="utf-8")
    heading_match = re.search(
        r"### 11\.2 Expiry — .*?(\b\w+\b) call sites",
        architecture_text,
    )
    assert heading_match is not None, "architecture §11.2 no longer states a count"

    documented_count = CALL_SITE_COUNT_WORDS.get(heading_match.group(1).lower())
    assert documented_count is not None, (
        f"unrecognised call-site count word: {heading_match.group(1)!r}"
    )
    assert documented_count == len(_expire_due_refunds_for_customer_call_sites()), (
        f"architecture §11.2 claims {documented_count} call sites; the code has "
        f"{_expire_due_refunds_for_customer_call_sites()}"
    )


def test_architecture_does_not_claim_expiry_is_the_first_statement() -> None:
    """Mutation: restore prose claiming expiry is the first statement in the loop."""
    architecture_text = ARCHITECTURE_DOCUMENT.read_text(encoding="utf-8")
    section_match = re.search(
        r"### 11\.2 Expiry.*?(?=\n### )",
        architecture_text,
        flags=re.DOTALL,
    )
    assert section_match is not None
    assert "first statement" not in section_match.group(0)

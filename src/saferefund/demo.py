# ruff: noqa: E402
"""A small, reproducible conversation with the SafeRefundAgent server.

Run this module when reading the project for the first time. It deliberately
does not manufacture an audit trail or load a fixture containing one: Sophie
sends an email to the real HTTP endpoint, the endpoint runs the real agent
loop, and each event below is returned by that endpoint after it was written.

The complete high-level flow is:

1. Reset SQLite and seed Sophie as a verified customer with a €249 damaged
   espresso-machine order.
2. Send Sophie's refund email to ``POST /inbound-email``.
3. The endpoint opens a case for the recognised customer.
4. The endpoint runs ``HeuristicModel`` for that open case.
5. The model proposes, in order: list orders, link ``ORD-1001``, refund
   €249, send a confirmation, and finish the case. The server checks every
   proposal before performing it.
6. The endpoint returns the completed case and its audit trail; this script
   prints that response with a plain-language explanation for each event.

The individual agent turns happen inside the endpoint call, rather than as
separate calls in this script. This demo therefore shows the real HTTP
integration path and its resulting audit trail, not an interactive protocol.
"""

from __future__ import annotations

import json
import sys
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The current Starlette TestClient emits this unrelated dependency warning once
# per short-lived model process. Keep the walkthrough itself readable.
warnings.filterwarnings(
    "ignore",
    message="Using `httpx` with `starlette.testclient` is deprecated.*",
)

from fastapi.testclient import TestClient

from saferefund import clock, config, ids
from saferefund.adapters import reset_adapters
from saferefund.agent import HeuristicModel
from saferefund.db import ORD_1001_ID, SOPHIE_EMAIL, SOPHIE_NAME, seed
from saferefund.main import create_app
from saferefund.models import CaseStatus

DEMO_FIXED_NOW = datetime(2030, 1, 15, 9, 30, tzinfo=UTC)
DEMO_MESSAGE_ID = "msg-demo-sophie-refund"
DATABASE_PATH = Path("saferefund.db")


def reset_demo_primitives() -> None:
    """Make the database, clock, identifiers, and adapters repeatable."""
    ids.reset_counter_for_tests()
    clock.reset_now_for_tests()
    clock.set_now_for_tests(DEMO_FIXED_NOW)
    reset_adapters()
    if DATABASE_PATH.exists():
        DATABASE_PATH.unlink()


def say(speaker: str, message: object) -> None:
    """Print one turn of the conversation without hiding structured data."""
    if isinstance(message, (dict, list)):
        rendered = json.dumps(message, indent=2, sort_keys=True, default=str)
    else:
        rendered = str(message)
    print(f"\n{speaker}\n{rendered}")


def describe_events(events: list[dict[str, Any]]) -> None:
    """Explain each server-created event as a transition in the case flow."""
    explanation = {
        "case_opened": (
            "Server state: opens a new case for the recognised customer; "
            "it starts open, with no order selected."
        ),
        "email_received": (
            "Server state: keeps Sophie's original message as the reason for "
            "the case. It is input, not permission to refund."
        ),
        "orders_listed": (
            "Agent -> server: get_orders. Server state: Sophie's owned orders "
            "are now visible to the agent for this case."
        ),
        "order_linked": (
            "Agent -> server: link_order. The policy confirms that the chosen "
            "order belongs to Sophie before making it this case's order."
        ),
        "refund_executed": (
            "Agent -> server: propose_refund. The policy permits this amount, "
            "so the server performs the payment and records its provider reference."
        ),
        "reply_sent": (
            "Agent -> server: send_reply. The server sends the confirmation to "
            "Sophie's database email address, not an address supplied by the agent."
        ),
        "case_closed": (
            "Agent -> server: finish. The successful refund and its customer "
            "reply are complete, so the server closes the case."
        ),
    }
    say("What changed during the server's agent loop:", "")
    for event in events:
        detail = json.dumps(event["detail"], sort_keys=True, default=str)
        print(f"  {event['id']}. {explanation.get(event['type'], event['type'])}")
        print(f"     Event recorded: {event['type']} {detail}")


def introduce_sophie() -> None:
    """State the prior facts that explain why this specific flow can proceed."""
    say(
        "Before Sophie writes:",
        (
            f"- {SOPHIE_NAME} ({SOPHIE_EMAIL}) is already a verified customer.\n"
            f"- She owns {ORD_1001_ID}: a delivered, damaged espresso machine "
            "costing €249.00.\n"
            "- There is no open case or previous refund for this request.\n"
            f"- Refunds at or below €{config.REFUND_APPROVAL_THRESHOLD:.2f} may be "
            "executed automatically; larger amounts need an operator.\n"
            "- The agent can propose actions, but the server checks the customer, "
            "order ownership, and refund amount before changing anything."
        ),
    )


def run_demo() -> int:
    """Let Sophie ask for a refund and show the complete HTTP conversation."""
    reset_demo_primitives()

    # The demo owns its database. Sophie and her delivered, damaged order are
    # trusted seed data; every case and audit event is created below.
    from saferefund import db

    db.reset_database()
    with db.SessionLocal() as session:
        seed(session)
        session.commit()

    introduce_sophie()

    # This is deliberately an ASGI integration client, not a service-function
    # call. The endpoint invokes the same gate and agent loop as `make run`.
    request = {
        "envelope_from": SOPHIE_EMAIL,
        "message_id": DEMO_MESSAGE_ID,
        "subject": "Refund please",
        "body": "My espresso machine arrived damaged.",
    }
    app = create_app(model=HeuristicModel())
    with TestClient(app) as client:
        say("Sophie -> POST /inbound-email", request)
        response = client.post("/inbound-email", json=request)

    response.raise_for_status()
    reply = response.json()
    say("Server -> Sophie", {key: reply[key] for key in ("case_id", "status")})
    describe_events(reply["audit_trail"])

    # The endpoint has already committed. These assertions keep the prose
    # honest: this particular request must complete a low-value refund.
    if reply["status"] != CaseStatus.CLOSED.value:
        print(f"\nExpected a closed case, got {reply['status']!r}.")
        return 1
    if not any(event["type"] == "refund_executed" for event in reply["audit_trail"]):
        print("\nExpected the server to record an executed refund.")
        return 1
    return 0


def main() -> None:
    """Entry point for `python -m saferefund.demo` and `make demo`."""
    sys.exit(run_demo())


if __name__ == "__main__":
    main()

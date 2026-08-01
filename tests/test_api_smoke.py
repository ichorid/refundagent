"""HTTP endpoint shapes and status codes."""

from helpers import open_case, propose_refund_action, sophie_message_id
from saferefund.db import ORD_1003_ID, SOPHIE_EMAIL, UNKNOWN_SENDER_EMAIL
from saferefund.service import run_agent_action


def test_inbound_email_known_sender(seeded_client) -> None:
    """Known senders receive case id, status, and audit trail."""
    response = seeded_client.post(
        "/inbound-email",
        json={
            "envelope_from": SOPHIE_EMAIL,
            "message_id": sophie_message_id(),
            "subject": "Refund",
            "body": "Damaged goods",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["case_id"]
    assert payload["status"]
    assert isinstance(payload["audit_trail"], list)
    assert payload["audit_trail"][0]["type"] == "case_opened"


def test_inbound_unknown_sender_202(seeded_client) -> None:
    """Unknown senders get a canned reply without opening a case."""
    response = seeded_client.post(
        "/inbound-email",
        json={
            "envelope_from": UNKNOWN_SENDER_EMAIL,
            "message_id": "msg-unknown",
            "subject": "Hi",
            "body": "Help",
        },
    )
    assert response.status_code == 202
    assert response.json() == {"status": "unknown_sender"}


def test_operator_pending_shape(seeded_session, client) -> None:
    """Pending endpoint returns refund metadata for operator review."""
    session = seeded_session
    case = open_case(session, message_id="msg-api-pending")
    case.linked_order_id = ORD_1003_ID
    session.flush()
    run_agent_action(session, case, propose_refund_action("600.00"))
    session.commit()
    response = client.get("/operator/pending")
    assert response.status_code == 200
    pending = response.json()["pending_refunds"]
    assert pending
    row = pending[0]
    assert {
        "refund_id",
        "case_id",
        "order_id",
        "amount",
        "approval_expires_at",
    } <= row.keys()


def test_verification_confirm_404(seeded_client) -> None:
    """Unknown verification tokens return 404."""
    response = seeded_client.post(
        "/verification/confirm",
        json={"token": "vtok_missing"},
    )
    assert response.status_code == 404


def test_verification_confirm_200(seeded_client) -> None:
    """Valid verification tokens return customer id and resumed cases."""
    inbound = seeded_client.post(
        "/inbound-email",
        json={
            "envelope_from": "tom@example.com",
            "message_id": "msg-tom-verify",
            "subject": "Verify me",
            "body": "Please verify",
        },
    )
    assert inbound.status_code == 200
    from saferefund import adapters

    verification_mail = next(
        message
        for message in adapters.mailer.outbox
        if "verification token" in message.body.lower()
    )
    token = verification_mail.body.rsplit(": ", 1)[-1]
    response = seeded_client.post("/verification/confirm", json={"token": token})
    assert response.status_code == 200
    payload = response.json()
    assert payload["customer_id"] == "cust_tom"
    assert payload["resumed_case_ids"]

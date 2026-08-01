from saferefund.domain.enums import Actor, Channel
from saferefund.domain.events import EventType
from saferefund.projections.customer import project_customer_summary
from tests.unit.projection_helpers import (
    BASE_TIME,
    customer_seed,
    customer_verified_event,
    fold_event,
)


def test_unverified_customer_without_verification_event() -> None:
    summary = project_customer_summary(
        customer_seed(customer_id="cust_tom", email="tom@example.com"),
        [],
        BASE_TIME,
    )

    assert summary.verified is False
    assert summary.customer_id == "cust_tom"
    assert summary.email == "tom@example.com"


def test_customer_verified_event_sets_verified_true() -> None:
    summary = project_customer_summary(
        customer_seed(),
        [customer_verified_event(seq=1)],
        BASE_TIME,
    )

    assert summary.verified is True


def test_customer_projection_ignores_other_customers_events() -> None:
    summary = project_customer_summary(
        customer_seed(customer_id="cust_tom", email="tom@example.com"),
        [customer_verified_event(seq=1, customer_id="cust_sophie")],
        BASE_TIME,
    )

    assert summary.verified is False


def test_customer_projection_ignores_non_customer_scoped_events() -> None:
    summary = project_customer_summary(
        customer_seed(),
        [
            fold_event(
                seq=1,
                event_type=EventType.EMAIL_RECEIVED,
                actor=Actor.CUSTOMER,
                case_id="case_1",
                channel=Channel.EMAIL,
                payload={
                    "message_id": "msg-1",
                    "subject": "ignored",
                    "body": "ignored",
                },
            )
        ],
        BASE_TIME,
    )

    assert summary.verified is False

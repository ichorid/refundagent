"""Small builders shared across tests."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from saferefund import ids
from saferefund.actions import (
    GetOrders,
    LinkOrder,
    ProposeRefund,
    RequestVerification,
    SendReply,
)
from saferefund.db import ORD_1001_ID, SOPHIE_CUSTOMER_ID
from saferefund.models import Case, CaseStatus, Customer, Order

FIXED_NOW = datetime(2030, 1, 15, 9, 30, tzinfo=UTC)


def get_orders_action() -> GetOrders:
    """Typed get_orders proposal."""
    return GetOrders(action="get_orders")


def link_order_action(order_id: str = ORD_1001_ID) -> LinkOrder:
    """Typed link_order proposal."""
    return LinkOrder(action="link_order", order_id=order_id)


def propose_refund_action(amount: Decimal | str) -> ProposeRefund:
    """Typed propose_refund proposal."""
    value = amount if isinstance(amount, Decimal) else Decimal(amount)
    return ProposeRefund(action="propose_refund", amount=value)


def send_reply_action(subject: str, body: str) -> SendReply:
    """Typed send_reply proposal."""
    return SendReply(action="send_reply", subject=subject, body=body)


def request_verification_action() -> RequestVerification:
    """Typed request_verification proposal."""
    return RequestVerification(action="request_verification")


def open_case(
    session: Session,
    *,
    customer_id: str = SOPHIE_CUSTOMER_ID,
    message_id: str = "msg-test-1",
) -> Case:
    """Insert one open case row."""
    case = Case(
        id=ids.case_id(),
        customer_id=customer_id,
        opening_message_id=message_id,
        status=CaseStatus.OPEN.value,
        created_at=FIXED_NOW,
    )
    session.add(case)
    session.flush()
    return case


def link_order(session: Session, case: Case, order_id: str = ORD_1001_ID) -> None:
    """Attach an order to a case without running the gate."""
    case.linked_order_id = order_id
    session.flush()


def customer_email(session: Session, customer_id: str) -> str:
    """Return the stored email for a customer id."""
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise LookupError(f"Customer not found: {customer_id}")
    return customer.email


def order_total(session: Session, order_id: str) -> Decimal:
    """Return the order total from the database."""
    order = session.get(Order, order_id)
    if order is None:
        raise LookupError(f"Order not found: {order_id}")
    return order.total


def tom_message_id() -> str:
    """Distinct opening message id for Tom's cases."""
    return "msg-tom-test"


def sophie_message_id() -> str:
    """Distinct opening message id for Sophie's cases."""
    return "msg-sophie-test"

"""Rx lab-order lifecycle validation (Real Order Management, replacing the
"/orders/" sidebar placeholder). `RxLabOrder.status` is a plain string, not a
DB enum, matching this app's established convention for lifecycle status
columns (DiagnosticOrder.status, WaitlistEntry.status) -- validation lives
here, in the service/route layer, not the database.

Mirrors ehr.services.diagnostic_orders: `transition()` takes an
already-loaded ORM object and mutates it in place, leaving the caller to
db.commit() -- no session lookups happen here.
"""
from __future__ import annotations

from datetime import datetime

PLACED = "placed"
IN_FABRICATION = "in_fabrication"
SHIPPED = "shipped"
RECEIVED = "received"
DISPENSED = "dispensed"
CANCELLED = "cancelled"

# Permissive skip-ahead transitions (a lab may not report every intermediate
# stage), same posture as diagnostic_orders.ALLOWED_TRANSITIONS.
ALLOWED_TRANSITIONS = {
    PLACED: {IN_FABRICATION, SHIPPED, RECEIVED, DISPENSED, CANCELLED},
    IN_FABRICATION: {SHIPPED, RECEIVED, DISPENSED, CANCELLED},
    SHIPPED: {RECEIVED, DISPENSED, CANCELLED},
    RECEIVED: {DISPENSED, CANCELLED},
    DISPENSED: set(),
    CANCELLED: set(),
}


def transition(order, new_status: str, *, dispensed_by_user_id: int = None, cancelled_reason: str = None):
    """Validates `order.status -> new_status` against ALLOWED_TRANSITIONS and
    stamps the fields that go with each destination state. Raises ValueError
    on an invalid transition (including a no-op transition to the same
    state) -- the caller turns that into a 400/flash error, same pattern as
    this app's other user-facing validation."""
    allowed = ALLOWED_TRANSITIONS.get(order.status, set())
    if new_status not in allowed:
        raise ValueError(f"Cannot transition a '{order.status}' lab order to '{new_status}'.")
    order.status = new_status
    if new_status == SHIPPED:
        order.shipped_at = datetime.utcnow()
    elif new_status == RECEIVED:
        order.received_at = datetime.utcnow()
    elif new_status == DISPENSED:
        order.dispensed_at = datetime.utcnow()
        order.dispensed_by_user_id = dispensed_by_user_id
    elif new_status == CANCELLED:
        order.cancelled_at = datetime.utcnow()
        order.cancelled_reason = cancelled_reason

"""Diagnostic-order lifecycle validation (Phase 3 of the chief-complaint/
CPT/billing-flow plan, BUILD_BACKLOG.md 0a). `DiagnosticOrder.status` is a
plain string, not a DB enum, matching this app's established convention for
lifecycle status columns (WaitlistEntry.status, AppointmentTest.status) --
validation lives here, in the service/route layer, not the database.

Mirrors this app's other small service modules (ap_composer.py,
cpt_mapper.py) in staying a thin, dependency-light layer: `transition()`
takes an already-loaded ORM object and mutates it in place, leaving the
caller to `db.commit()` -- no session lookups happen here.
"""
from __future__ import annotations

from datetime import datetime

ORDERED = "ordered"
SCHEDULED = "scheduled"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
CANCELLED = "cancelled"

ALLOWED_TRANSITIONS = {
    ORDERED: {SCHEDULED, IN_PROGRESS, COMPLETED, CANCELLED},
    SCHEDULED: {IN_PROGRESS, COMPLETED, CANCELLED},
    IN_PROGRESS: {COMPLETED, CANCELLED},
    COMPLETED: set(),
    CANCELLED: set(),
}


def transition(order, new_status: str, *, completed_exam_id: int = None, completed_by_user_id: int = None,
               result_summary: str = None, cancelled_reason: str = None, scheduled_appointment_id: int = None):
    """Validates `order.status -> new_status` against ALLOWED_TRANSITIONS and
    stamps the fields that go with each destination state. Raises ValueError
    on an invalid transition (including a no-op transition to the same
    state) -- the caller turns that into a 400/flash error, same pattern as
    this app's other user-facing validation."""
    allowed = ALLOWED_TRANSITIONS.get(order.status, set())
    if new_status not in allowed:
        raise ValueError(f"Cannot transition a '{order.status}' order to '{new_status}'.")
    order.status = new_status
    if new_status == SCHEDULED:
        order.scheduled_appointment_id = scheduled_appointment_id
    elif new_status == COMPLETED:
        order.completed_at = datetime.utcnow()
        order.completed_exam_id = completed_exam_id
        order.completed_by_user_id = completed_by_user_id
        order.result_summary = result_summary
    elif new_status == CANCELLED:
        order.cancelled_at = datetime.utcnow()
        order.cancelled_reason = cancelled_reason

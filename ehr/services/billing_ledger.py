"""Service & Fee Catalog, Phase 2 (user request: a live running total of
"today's services and materials" on the patient overview, replacing the old
"Pending Exam: N/A" placeholder). Staff-facing estimate only -- this app has
no billing/claims infrastructure, and nothing this module computes is ever
transmitted, submitted, or billed to a payer, same posture as the existing
CPT Billing Preview (ehr.services.cpt_mapper).

"Today" is scoped to the patient's own non-cancelled appointment(s) whose
scheduled_at falls on today's UTC calendar date -- this app has no other
notion of a patient's current visit, and every other timestamp in the app
(datetime.utcnow()) is naive/UTC-implicit, so this matches that convention
rather than introducing a separate timezone-aware "visit day" concept.
"""
from __future__ import annotations

from datetime import datetime, timedelta, time
from sqlalchemy.orm import Session

from ehr.models.database import Appointment, AppointmentStatus, Service, VisitCharge


def _todays_appointment_ids(db: Session, patient_id: int) -> list[int]:
    today_start = datetime.combine(datetime.utcnow().date(), time.min)
    today_end = today_start + timedelta(days=1)
    rows = (db.query(Appointment.id)
        .filter(Appointment.patient_id == patient_id,
                Appointment.scheduled_at >= today_start, Appointment.scheduled_at < today_end,
                Appointment.status != AppointmentStatus.cancelled)
        .all())
    return [r[0] for r in rows]


def get_todays_charges(db: Session, patient_id: int):
    """Returns (total: float, line_items: list[VisitCharge]) for every
    non-voided charge on the patient's today's appointment(s), ordered
    oldest-first (the order they were added during the visit)."""
    appt_ids = _todays_appointment_ids(db, patient_id)
    if not appt_ids:
        return 0.0, []
    line_items = (db.query(VisitCharge)
        .filter(VisitCharge.appointment_id.in_(appt_ids), VisitCharge.voided == False)  # noqa: E712
        .order_by(VisitCharge.added_at).all())
    total = sum(c.unit_fee * c.quantity for c in line_items)
    return total, line_items


def todays_appointment_for_manual_add(db: Session, patient_id: int) -> Appointment | None:
    """The appointment a manually-added charge should attach to. If the
    patient has more than one non-cancelled appointment today (rare --
    e.g. a same-day recheck), this picks the earliest one; a documented
    simplification, not a claim that only one visit per day is possible."""
    appt_ids = _todays_appointment_ids(db, patient_id)
    if not appt_ids:
        return None
    return (db.query(Appointment).filter(Appointment.id.in_(appt_ids))
        .order_by(Appointment.scheduled_at).first())


def add_diagnostic_order_completion_charge(db: Session, order, patient_id: int, user_id):
    """Service & Fee Catalog, Phase 3 (user request: the day's bill
    accumulates automatically as tests/services are performed) -- completing
    a DiagnosticOrder (ehr.services.diagnostic_orders.transition to
    COMPLETED) auto-adds its linked Service's fee to whichever appointment
    the order belongs to: the one it was scheduled against
    (order.scheduled_appointment_id), or failing that, the patient's today's
    appointment (an order completed from the overview page's "Mark
    Complete" button, not tied to a specific scheduled visit). No-ops
    silently if neither exists, or the test has no linked Service -- same
    narrow-lookup posture as the rest of this catalog. Deliberately kept out
    of ehr.services.diagnostic_orders.transition() itself, which is a
    dependency-light module with no session lookups by design; this is the
    caller's job."""
    appointment_id = order.scheduled_appointment_id
    if not appointment_id:
        appt = todays_appointment_for_manual_add(db, patient_id)
        appointment_id = appt.id if appt else None
    if not appointment_id:
        return
    service = db.query(Service).filter(Service.diagnostic_test_id == order.diagnostic_test_id,
        Service.active == True).first()  # noqa: E712
    if not service:
        return
    already = (db.query(VisitCharge).filter(VisitCharge.appointment_id == appointment_id,
        VisitCharge.service_id == service.id, VisitCharge.source == "diagnostic_order_completed",
        VisitCharge.voided == False).first())  # noqa: E712
    if already:
        return
    db.add(VisitCharge(appointment_id=appointment_id, service_id=service.id, unit_fee=service.fee,
        source="diagnostic_order_completed", added_by_user_id=user_id))


def _add_charge_for_cpt(db: Session, appointment_id: int, cpt_code: str, user_id):
    service = db.query(Service).filter(Service.cpt_code == cpt_code, Service.active == True).first()  # noqa: E712
    if not service:
        return
    already = (db.query(VisitCharge).filter(VisitCharge.appointment_id == appointment_id,
        VisitCharge.service_id == service.id, VisitCharge.source == "em_code",
        VisitCharge.voided == False).first())  # noqa: E712
    if already:
        return
    db.add(VisitCharge(appointment_id=appointment_id, service_id=service.id, unit_fee=service.fee,
        source="em_code", added_by_user_id=user_id))


def add_exam_charges(db: Session, exam, user_id):
    """Service & Fee Catalog, Phase 3 (user request: the day's bill
    accumulates automatically as tests/services are performed) -- saving an
    exam auto-adds a charge for its exam-level CPT (92004/92014, the same
    relationship-driven code ehr.services.cpt_mapper's billing preview
    already computes) and, separately, its confirmed E/M code
    (exam.em_code_confirmed, e.g. 99213/99214), each if a matching active
    Service exists. No-ops entirely for a walk-in exam with no linked
    appointment (exam.appointment_id is None) -- same "nothing to attach a
    charge to" gate cpt_mapper.compute_cpt_summary already uses."""
    if not exam.appointment_id or not exam.appointment:
        return
    relationship = exam.appointment.patient_relationship_at_booking or "established"
    exam_cpt = "92004" if relationship == "new" else "92014"
    _add_charge_for_cpt(db, exam.appointment_id, exam_cpt, user_id)
    if exam.em_code_confirmed:
        _add_charge_for_cpt(db, exam.appointment_id, exam.em_code_confirmed, user_id)

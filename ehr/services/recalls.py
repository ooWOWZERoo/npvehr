"""Recall worklist logic: who is due back, what staff have done about it, and closing a recall.

A recall (PatientRecall, loaded by the recall import or entered later) is a due-back date. This module only organises the
follow-up work: it lists open recalls by urgency, shows context that helps decide whether anyone needs calling (an
appointment already booked in this system, a newer exam on file, the last contact attempt), and records what staff do:
a contact attempt, closing it as satisfied or dismissed, or reopening it. History is append-only (RecallAction);
PatientRecall.status holds the current state. Nothing here sends any message or contacts anyone.
"""
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ehr.models.database import Appointment, AppointmentStatus, EyeExam, Patient
from ehr.models.imports import PatientRecall, RecallAction
from ehr.services import authz, field_audit

WINDOWS = {"30": "Overdue and due in the next 30 days", "overdue": "Overdue only", "90": "Overdue and due in the next 90 days", "all": "All, any due date"}
STATUSES = {"open": "Open", "satisfied": "Satisfied", "dismissed": "Dismissed", "all": "All"}
METHODS = {"phone": "Phone", "text": "Text", "email": "Email", "mail": "Mail", "portal": "Portal message", "in_person": "In person"}
ACTIVE_APPT = (AppointmentStatus.scheduled, AppointmentStatus.checked_in, AppointmentStatus.in_progress)
SEEN_SLACK_DAYS = 60            # an exam this long before the due date (or any time after) counts as "seen"


def worklist_query(db: Session, user, status: str = "open", window: str = "30", rtype: str = "", q: str = "",
                   hide_booked: bool = False, today: Optional[date] = None):
    today = today or date.today()
    query = (db.query(PatientRecall, Patient).join(Patient, Patient.id == PatientRecall.patient_id))
    if authz.is_patient_restricted(user):
        query = query.filter(PatientRecall.patient_id.in_(authz.own_patient_ids(db, user.provider_id) or {0}))
    if status in ("open", "satisfied", "dismissed"):
        query = query.filter(PatientRecall.status == status)
    if window == "overdue":
        query = query.filter(PatientRecall.due_date < today)
    elif window in ("30", "90"):
        query = query.filter(PatientRecall.due_date <= today + timedelta(days=int(window)))
    if rtype:
        query = query.filter(PatientRecall.recall_type == rtype)
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter((Patient.last_name.ilike(like)) | (Patient.first_name.ilike(like)) | (Patient.phone.ilike(like)))
    if hide_booked:
        booked = db.query(Appointment.patient_id).filter(Appointment.scheduled_at >= datetime.combine(today, datetime.min.time()),
                                                         Appointment.status.in_(ACTIVE_APPT))
        query = query.filter(~PatientRecall.patient_id.in_(booked))
    return query.order_by(PatientRecall.due_date, PatientRecall.id)


def recall_types(db: Session) -> list:
    return [t for (t,) in db.query(PatientRecall.recall_type).distinct().order_by(PatientRecall.recall_type).all()]


def counts(db: Session, user, today: Optional[date] = None) -> dict:
    """Headline numbers for the worklist tiles (open recalls only, same record-level scope as the list)."""
    today = today or date.today()
    base = worklist_query(db, user, "open", "all", today=today)
    return {"overdue": base.filter(PatientRecall.due_date < today).count(),
            "soon": base.filter(PatientRecall.due_date >= today, PatientRecall.due_date <= today + timedelta(days=30)).count(),
            "open": base.count()}


def context_for(db: Session, rows: list, today: Optional[date] = None) -> dict:
    """Per-row context for a page of (recall, patient) rows: {recall_id: {...}}. Small indexed queries on just this page."""
    today = today or date.today()
    recall_ids = [r.id for r, _ in rows]
    patient_ids = list({p.id for _, p in rows})
    contacts = {}
    for rid, n, last in (db.query(RecallAction.recall_id, func.count(RecallAction.id), func.max(RecallAction.created_at))
                         .filter(RecallAction.recall_id.in_(recall_ids or [0]), RecallAction.action == "contacted").group_by(RecallAction.recall_id).all()):
        contacts[rid] = (n, last)
    last_method = {}
    for a in (db.query(RecallAction).filter(RecallAction.recall_id.in_(recall_ids or [0]), RecallAction.action == "contacted")
              .order_by(RecallAction.id).all()):
        last_method[a.recall_id] = a.method
    booked = dict(db.query(Appointment.patient_id, func.min(Appointment.scheduled_at))
                  .filter(Appointment.patient_id.in_(patient_ids or [0]), Appointment.scheduled_at >= datetime.combine(today, datetime.min.time()),
                          Appointment.status.in_(ACTIVE_APPT)).group_by(Appointment.patient_id).all())
    latest_exam = dict(db.query(EyeExam.patient_id, func.max(EyeExam.exam_date)).filter(EyeExam.patient_id.in_(patient_ids or [0]))
                       .group_by(EyeExam.patient_id).all())
    out = {}
    for r, p in rows:
        n, last = contacts.get(r.id, (0, None))
        seen = latest_exam.get(p.id)
        cutoff = (r.due_date - timedelta(days=SEEN_SLACK_DAYS)).isoformat()
        out[r.id] = {"contacts": n, "last_contacted": last, "last_method": last_method.get(r.id), "booked": booked.get(p.id),
                     "seen": seen if seen and seen >= cutoff else None}
    return out


# ------------------------------------------------------------------------------------------ recording
def _clean(v: str, n: int = 255) -> Optional[str]:
    v = (v or "").strip()
    return v[:n] or None


def log_contact(db: Session, recall: PatientRecall, user_id: int, method: str, note: str = "") -> Optional[RecallAction]:
    if method not in METHODS:
        return None
    a = RecallAction(recall_id=recall.id, action="contacted", method=method, note=_clean(note), user_id=user_id)
    db.add(a)
    db.flush()
    return a


def close(db: Session, recall: PatientRecall, status: str, user_id: int, note: str = "") -> Optional[str]:
    """Closes an open recall as 'satisfied' or 'dismissed'. Returns an error message, or None on success.
    A dismissal needs a reason (it is the only way a recall leaves the list without the patient being seen)."""
    if status not in ("satisfied", "dismissed"):
        return "Unknown action."
    if recall.status != "open":
        return "That recall is already closed."
    note = _clean(note)
    if status == "dismissed" and (not note or len(note) < 5):
        return "Give a reason for dismissing a recall."
    old = recall.status
    recall.status = status
    db.add(RecallAction(recall_id=recall.id, action=status, note=note, user_id=user_id))
    field_audit.record_field_changes(db, "patient_recalls", recall.id, {"status": old}, {"status": status}, user_id)
    db.flush()
    return None


def reopen(db: Session, recall: PatientRecall, user_id: int, note: str = "") -> bool:
    if recall.status == "open":
        return False
    old = recall.status
    recall.status = "open"
    db.add(RecallAction(recall_id=recall.id, action="reopened", note=_clean(note), user_id=user_id))
    field_audit.record_field_changes(db, "patient_recalls", recall.id, {"status": old}, {"status": "open"}, user_id)
    db.flush()
    return True

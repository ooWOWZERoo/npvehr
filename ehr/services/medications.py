"""Medication / allergy list logic (design: PATIENT_MEDICATION_LIST_DESIGN.md).

The one place a drug name is matched to a class, and a safety flag is derived from the list:

    a flag type is DERIVED 'yes' for a patient when
        the patient has an ACTIVE medication whose name contains (whole word/phrase, case-insensitive)
        a REVIEWED term of an ACTIVE class that is linked to that flag type.

Unreviewed terms and inactive classes classify nothing. Matching is plain text -- no brand/generic knowledge
beyond the terms the clinicians enter -- so medications that match no reviewed term are reported as
"not classified" rather than silently treated as safe. A list that has never been reviewed is UNKNOWN, not
"none": an empty list means "no medications" only after a review row says the patient reports none.
Everything is decision support; nothing is transmitted or changes a note, billing, or an order.
"""
import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from ehr.models.medications import (MedicationClass, MedicationClassTerm, MedicationListReview, PatientAllergy,
                                    PatientMedication, SafetyFlagClassLink)
from ehr.services import field_audit

log = logging.getLogger(__name__)
STALE_MONTHS = 12
MED_FIELDS = ["name", "strength", "route", "frequency", "eye", "indication", "status", "start_date", "stop_date", "note"]
ALLERGY_FIELDS = ["allergen", "reaction", "severity", "status", "note"]
EYES = ("OD", "OS", "OU")
SEVERITIES = ("mild", "moderate", "severe")


def term_matches(term: str, name: str) -> bool:
    t = (term or "").strip().lower()
    return bool(t) and re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", (name or "").lower()) is not None


def active_medications(db: Session, patient_id: int) -> list:
    return (db.query(PatientMedication).filter(PatientMedication.patient_id == patient_id, PatientMedication.status == "active")
            .order_by(PatientMedication.name).all())


def classify(db: Session, meds: list) -> dict:
    """{medication id: [class labels]} using reviewed terms of active classes only."""
    terms = (db.query(MedicationClassTerm.term, MedicationClass.label).join(MedicationClass, MedicationClass.id == MedicationClassTerm.class_id)
             .filter(MedicationClassTerm.reviewed.is_(True), MedicationClass.is_active.is_(True)).all())
    return {m.id: sorted({label for term, label in terms if term_matches(term, m.name)}) for m in meds}


def derived_flags(db: Session, patient_id: int) -> dict:
    """{flag_type_id: [medication names]} -- flags the medication list says are 'yes'. Never raises."""
    try:
        meds = active_medications(db, patient_id)
        if not meds:
            return {}
        links = (db.query(SafetyFlagClassLink.flag_type_id, MedicationClassTerm.term)
                 .join(MedicationClass, MedicationClass.id == SafetyFlagClassLink.class_id)
                 .join(MedicationClassTerm, MedicationClassTerm.class_id == MedicationClass.id)
                 .filter(MedicationClass.is_active.is_(True), MedicationClassTerm.reviewed.is_(True)).all())
    except Exception:                                  # noqa: BLE001 -- decision support must not break a page
        log.exception("Could not derive medication-based flags for patient %s", patient_id)
        return {}
    out = {}
    for flag_id, term in links:
        for m in meds:
            if term_matches(term, m.name) and m.name not in out.setdefault(flag_id, []):
                out[flag_id].append(m.name)
    return out


# ------------------------------------------------------------------------------------------ editing
def _clean(v, n):
    v = (v or "").strip()
    return v[:n] or None


def _date(v):
    try:
        return date.fromisoformat((v or "").strip()) if (v or "").strip() else None
    except ValueError:
        return None


def add_medication(db: Session, patient_id: int, user_id: int, form) -> Optional[PatientMedication]:
    name = _clean(form.get("name"), 160)
    if not name:
        return None
    eye = (form.get("eye") or "").upper()
    m = PatientMedication(patient_id=patient_id, name=name, strength=_clean(form.get("strength"), 80), route=_clean(form.get("route"), 40),
                          frequency=_clean(form.get("frequency"), 80), eye=eye if eye in EYES else None,
                          indication=_clean(form.get("indication"), 160), status="active", start_date=_date(form.get("start_date")),
                          source="staff", note=_clean(form.get("note"), 255), recorded_by_user_id=user_id)
    db.add(m)
    db.flush()
    field_audit.record_field_changes(db, "patient_medications", m.id, {}, {"name": name, "status": "active"}, user_id)
    return m


def update_medication(db: Session, med: PatientMedication, user_id: int, form) -> bool:
    name = _clean(form.get("name"), 160)
    if not name:
        return False
    before = {f: getattr(med, f) for f in MED_FIELDS}
    eye = (form.get("eye") or "").upper()
    med.name, med.strength, med.route = name, _clean(form.get("strength"), 80), _clean(form.get("route"), 40)
    med.frequency, med.eye, med.indication = _clean(form.get("frequency"), 80), (eye if eye in EYES else None), _clean(form.get("indication"), 160)
    med.start_date, med.note = _date(form.get("start_date")), _clean(form.get("note"), 255)
    field_audit.record_field_changes(db, "patient_medications", med.id, before, {f: getattr(med, f) for f in MED_FIELDS}, user_id)
    return True


def set_medication_status(db: Session, med: PatientMedication, status: str, user_id: int) -> None:
    before = {f: getattr(med, f) for f in MED_FIELDS}
    med.status = status
    med.stop_date = date.today() if status == "stopped" else None
    field_audit.record_field_changes(db, "patient_medications", med.id, before, {f: getattr(med, f) for f in MED_FIELDS}, user_id)


def add_allergy(db: Session, patient_id: int, user_id: int, form) -> Optional[PatientAllergy]:
    allergen = _clean(form.get("allergen"), 160)
    if not allergen:
        return None
    sev = (form.get("severity") or "").lower()
    a = PatientAllergy(patient_id=patient_id, allergen=allergen, reaction=_clean(form.get("reaction"), 160),
                       severity=sev if sev in SEVERITIES else None, status="active", note=_clean(form.get("note"), 255),
                       recorded_by_user_id=user_id)
    db.add(a)
    db.flush()
    field_audit.record_field_changes(db, "patient_allergies", a.id, {}, {"allergen": allergen, "status": "active"}, user_id)
    return a


def update_allergy(db: Session, al: PatientAllergy, user_id: int, form) -> bool:
    allergen = _clean(form.get("allergen"), 160)
    if not allergen:
        return False
    before = {f: getattr(al, f) for f in ALLERGY_FIELDS}
    sev = (form.get("severity") or "").lower()
    al.allergen, al.reaction, al.severity, al.note = allergen, _clean(form.get("reaction"), 160), (sev if sev in SEVERITIES else None), _clean(form.get("note"), 255)
    field_audit.record_field_changes(db, "patient_allergies", al.id, before, {f: getattr(al, f) for f in ALLERGY_FIELDS}, user_id)
    return True


def set_allergy_status(db: Session, al: PatientAllergy, status: str, user_id: int) -> None:
    before = {f: getattr(al, f) for f in ALLERGY_FIELDS}
    al.status = status
    field_audit.record_field_changes(db, "patient_allergies", al.id, before, {f: getattr(al, f) for f in ALLERGY_FIELDS}, user_id)


# ------------------------------------------------------------------------------------------ reviews
def count_active(db: Session, patient_id: int, kind: str) -> int:
    if kind == "medications":
        return db.query(PatientMedication).filter(PatientMedication.patient_id == patient_id, PatientMedication.status == "active").count()
    return db.query(PatientAllergy).filter(PatientAllergy.patient_id == patient_id, PatientAllergy.status == "active").count()


def record_review(db: Session, patient_id: int, kind: str, outcome: str, user_id: int, exam_id: Optional[int] = None):
    """Returns the review row, or None if the request is inconsistent ('none_reported' with active items still listed)."""
    if kind not in ("medications", "allergies") or outcome not in ("no_changes", "updated", "none_reported"):
        return None
    n = count_active(db, patient_id, kind)
    if outcome == "none_reported" and n:
        return None
    r = MedicationListReview(patient_id=patient_id, kind=kind, outcome=outcome, exam_id=exam_id, item_count=n, reviewed_by_user_id=user_id)
    db.add(r)
    db.flush()
    return r


def last_review(db: Session, patient_id: int, kind: str):
    return (db.query(MedicationListReview).filter(MedicationListReview.patient_id == patient_id, MedicationListReview.kind == kind)
            .order_by(MedicationListReview.reviewed_at.desc(), MedicationListReview.id.desc()).first())


def review_status(db: Session, patient_id: int, kind: str, now: Optional[datetime] = None) -> dict:
    """{'review': row|None, 'state': 'never' | 'stale' | 'current'} -- stale after STALE_MONTHS or if items were
    added since a 'none reported' review (that outcome no longer matches the list)."""
    r = last_review(db, patient_id, kind)
    if r is None:
        return {"review": None, "state": "never"}
    now = now or datetime.utcnow()
    if now - r.reviewed_at > timedelta(days=30 * STALE_MONTHS):
        return {"review": r, "state": "stale"}
    if r.outcome == "none_reported" and count_active(db, patient_id, kind):
        return {"review": r, "state": "stale"}
    return {"review": r, "state": "current"}

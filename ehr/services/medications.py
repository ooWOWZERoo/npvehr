"""Medication / allergy list logic (design: PATIENT_MEDICATION_LIST_DESIGN.md).

The one place a drug name is matched to a class, and a safety flag is derived from the list:

    a flag type is DERIVED 'yes' for a patient when
        the patient has an ACTIVE medication whose name contains (whole word/phrase, case-insensitive)
        a REVIEWED term of an ACTIVE class that is linked to that flag type.

Unreviewed terms and inactive classes classify nothing. Matching is whole-word text against the name as recorded plus, for
a drug in the catalog, its generic name and known brand names (equivalent_names) -- so a term needs listing only once, under
either name -- and medications that match no reviewed term are reported as
"not classified" rather than silently treated as safe. A list that has never been reviewed is UNKNOWN, not
"none": an empty list means "no medications" only after a review row says the patient reports none.
Everything is decision support; nothing is transmitted or changes a note, billing, or an order.
"""
import logging
import re
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from ehr.models.drug_catalog import DrugAlias, DrugCatalogEntry
from ehr.models.medications import (MedicationClass, MedicationClassTerm, MedicationListReview, PatientAllergy,
                                    PatientMedication, SafetyFlagClassLink)
from ehr.services import field_audit

log = logging.getLogger(__name__)
STALE_MONTHS = 12
MED_FIELDS = ["name", "strength", "route", "frequency", "eye", "indication", "status", "start_date", "stop_date", "note"]
ALLERGY_FIELDS = ["allergen", "reaction", "severity", "status", "note"]
EYES = ("OD", "OS", "OU", "PO", "NA")             # PO = by mouth / systemic (oral); NA = not applicable (not an eye medication)
SEVERITIES = ("mild", "moderate", "severe")


def term_matches(term: str, name: str) -> bool:
    t = (term or "").strip().lower()
    return bool(t) and re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", (name or "").lower()) is not None


def active_medications(db: Session, patient_id: int) -> list:
    return (db.query(PatientMedication).filter(PatientMedication.patient_id == patient_id, PatientMedication.status == "active")
            .order_by(PatientMedication.name).all())


def equivalent_names(db: Session, meds: list) -> dict:
    """{medication id: [names to match terms against]}: the name as recorded, plus -- when the medication is a drug in our
    catalog (linked by the guided intake, or an older free-text row whose whole name is a catalog drug or brand name) -- that
    drug's generic name and every brand name known for it. So a term that lists "Coumadin" matches a patient recorded under
    warfarin, a term that lists "warfarin" matches one recorded under Coumadin, and Jantoven and Coumadin match each other.
    This only ever ADDS names to compare, and terms still count only once a reviewer has signed them off. Never raises: on any
    trouble a medication is matched on its recorded name alone."""
    names = {m.id: [m.name] for m in meds}
    try:
        base = {m.id: m.drug_id for m in meds if m.drug_id}
        unlinked = {m.id: (m.name or "").strip().lower() for m in meds if not m.drug_id and (m.name or "").strip()}
        if unlinked:
            keys = set(unlinked.values())
            by_name = dict((lower, id_) for id_, lower in db.query(DrugCatalogEntry.id, DrugCatalogEntry.name_lower)
                           .filter(DrugCatalogEntry.name_lower.in_(keys)).all())
            by_alias = dict((al, did) for al, did in db.query(DrugAlias.alias_lower, DrugAlias.drug_id).filter(DrugAlias.alias_lower.in_(keys)).all())
            for mid, key in unlinked.items():
                if key in by_name or key in by_alias:
                    base[mid] = by_name.get(key) or by_alias[key]
        if not base:
            return names
        drugs = {i: (n, nl) for i, n, nl in db.query(DrugCatalogEntry.id, DrugCatalogEntry.name, DrugCatalogEntry.name_lower)
                 .filter(DrugCatalogEntry.id.in_(set(base.values()))).all()}
        # a brand row's generics are the drugs that carry the brand's own name as an alias
        generics = {}
        for al, did in db.query(DrugAlias.alias_lower, DrugAlias.drug_id).filter(DrugAlias.alias_lower.in_({nl for _, nl in drugs.values()})).all():
            generics.setdefault(al, set()).add(did)
        group_of = {mid: {d} | generics.get(drugs[d][1], set()) for mid, d in base.items() if d in drugs}
        all_ids = set().union(*group_of.values()) if group_of else set()
        label = {i: n for i, n in db.query(DrugCatalogEntry.id, DrugCatalogEntry.name).filter(DrugCatalogEntry.id.in_(all_ids)).all()}
        brands = {}
        for did, alias in db.query(DrugAlias.drug_id, DrugAlias.alias).filter(DrugAlias.drug_id.in_(all_ids)).all():
            brands.setdefault(did, []).append(alias)
        for mid, group in group_of.items():
            for g in sorted(group):
                for n in [label.get(g)] + brands.get(g, []):
                    if n and n.lower() not in {x.lower() for x in names[mid]}:
                        names[mid].append(n)
    except Exception:                                  # noqa: BLE001 -- decision support must not break a page
        log.exception("Could not expand medication names for matching")
        return {m.id: [m.name] for m in meds}
    return names


def classify(db: Session, meds: list) -> dict:
    """{medication id: [class labels]} using reviewed terms of active classes only; a term matches any generic or brand name
    of the medication (equivalent_names)."""
    terms = (db.query(MedicationClassTerm.term, MedicationClass.label).join(MedicationClass, MedicationClass.id == MedicationClassTerm.class_id)
             .filter(MedicationClassTerm.reviewed.is_(True), MedicationClass.is_active.is_(True)).all())
    names = equivalent_names(db, meds)
    return {m.id: sorted({label for term, label in terms if any(term_matches(term, n) for n in names[m.id])}) for m in meds}


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
    names = equivalent_names(db, meds)
    out = {}
    for flag_id, term in links:
        for m in meds:
            if any(term_matches(term, n) for n in names[m.id]) and m.name not in out.setdefault(flag_id, []):
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


def add_resolved_medication(db: Session, patient_id: int, user_id: int, values: dict, indication=None, note=None, start_date=None) -> PatientMedication:
    """Create a medication from the guided intake. `values` is the output of drug_catalog.resolve_selection (already
    validated); the text columns are always filled so the record reads the same with or without the catalog link."""
    m = PatientMedication(patient_id=patient_id, name=values["name"], strength=values["strength"], route=values["route"],
                          frequency=values["frequency"], eye=values["eye"], indication=_clean(indication, 160), status="active",
                          start_date=_date(start_date), source="staff", note=_clean(note, 255), recorded_by_user_id=user_id,
                          drug_id=values["drug_id"], dosage_id=values["dosage_id"], frequency_id=values["frequency_id"],
                          drug_rxcui=values["drug_rxcui"], is_custom=values["is_custom"])
    db.add(m)
    db.flush()
    field_audit.record_field_changes(db, "patient_medications", m.id, {}, {"name": m.name, "status": "active", "eye": m.eye}, user_id)
    return m


def update_resolved_medication(db: Session, med: PatientMedication, user_id: int, values: dict, indication=None, note=None, start_date=None) -> PatientMedication:
    """Re-save a medication from the guided intake (values from drug_catalog.resolve_selection, already validated). Status and
    stop date are left alone; every changed field is written to the field-change audit trail."""
    before = {f: getattr(med, f) for f in MED_FIELDS}
    med.name, med.strength, med.route, med.frequency, med.eye = values["name"], values["strength"], values["route"], values["frequency"], values["eye"]
    med.drug_id, med.dosage_id, med.frequency_id = values["drug_id"], values["dosage_id"], values["frequency_id"]
    med.drug_rxcui, med.is_custom = values["drug_rxcui"], values["is_custom"]
    med.indication, med.note, med.start_date = _clean(indication, 160), _clean(note, 255), _date(start_date)
    field_audit.record_field_changes(db, "patient_medications", med.id, before, {f: getattr(med, f) for f in MED_FIELDS}, user_id)
    return med


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


def record_exam_reviews(db: Session, exam, form, user_id: int) -> int:
    """Records the reviews a clinician confirmed on the New Exam form's medications card (form fields
    med_review_medications / med_review_allergies = an outcome), linked to the exam just saved. Best-effort and
    savepoint-guarded like the ROS findings: a problem here can never stop the exam itself from saving. Returns how many
    reviews were recorded (an inconsistent request, e.g. 'none reported' for a list with items, is ignored)."""
    n = 0
    for kind in ("medications", "allergies"):
        outcome = (form.get(f"med_review_{kind}") or "").strip()
        if not outcome:
            continue
        try:
            with db.begin_nested():
                if record_review(db, exam.patient_id, kind, outcome, user_id, exam_id=exam.id):
                    n += 1
        except Exception:                              # noqa: BLE001 -- never block saving an exam
            log.exception("Could not record %s review for exam %s", kind, exam.id)
    return n


def reviews_for_exam(db: Session, exam_id: int) -> list:
    """[(review, 'First Last' or None)] recorded at this exam, for the exam page."""
    from ehr.models.database import User
    rows = (db.query(MedicationListReview, User).outerjoin(User, User.id == MedicationListReview.reviewed_by_user_id)
            .filter(MedicationListReview.exam_id == exam_id).order_by(MedicationListReview.id).all())
    return [(r, (f"{u.first_name} {u.last_name}".strip() if u else None)) for r, u in rows]


def exam_card_summary(db: Session, patient_id: int) -> dict:
    """Everything the exam form's card needs, in one small payload."""
    meds = active_medications(db, patient_id)
    allergies = (db.query(PatientAllergy).filter(PatientAllergy.patient_id == patient_id, PatientAllergy.status == "active")
                 .order_by(PatientAllergy.allergen).all())
    out = {"medications": [{"name": m.name, "strength": m.strength, "route": m.route, "eye": m.eye, "frequency": m.frequency} for m in meds],
           "allergies": [{"allergen": a.allergen, "reaction": a.reaction, "severity": a.severity} for a in allergies]}
    for kind in ("medications", "allergies"):
        st = review_status(db, patient_id, kind)
        out[f"{kind}_state"] = st["state"]
        out[f"{kind}_reviewed_at"] = st["review"].reviewed_at.strftime("%m/%d/%Y") if st["review"] else None
    return out

"""The structured part of the exam's Prescription & treatment step (v3.06): medications prescribed and tests ordered.

Medications prescribed
    The step reuses the guided medication intake (catalog pick or Add Custom, dosage, frequency, eye). The exam doesn't exist yet
    while the form is being filled in, so each choice is staged in the page and travels in one hidden JSON field (`prescribed_meds`).
    At save time the server re-validates every entry through the same rules as the Medications tab (drug_catalog.resolve_selection)
    and then adds it to the patient's medication list as an active medication with source 'prescribed', linked to the exam
    (patient_medications.prescribed_exam_id) and started on the exam date. Prescribing a medication that is already active with the same
    name, strength and eye replaces it: the earlier entry is stopped (audited, like any stop) so the list shows it once.

Orders
    Tests ticked in the step become real DiagnosticOrder rows (the same lifecycle as the Glaucoma panel and Return visit orders), tied
    to the exam; a test already ordered for this exam from another panel is not ordered twice. The NCCI billing check that guards the
    Return visit orders guards these too (see billing_override).

Nothing here transmits anything: a prescribed medication is a row in the patient's own medication list, not an e-prescription.
"""
import json
from typing import Optional

from sqlalchemy.orm import Session

from ehr.models.database import DiagnosticOrder, DiagnosticTest
from ehr.models.medications import PatientMedication
from ehr.services import drug_catalog as catalog
from ehr.services import medications as med_svc

MAX_PRESCRIBED = 20


class _Payload:
    """Attribute view of one staged entry, shaped like the API's MedicationIn so resolve_selection treats both the same."""
    def __init__(self, raw: dict):
        def num(k):
            try:
                return int(raw.get(k)) if raw.get(k) not in (None, "") else None
            except (TypeError, ValueError):
                raise ValueError("A prescribed medication has a malformed catalog choice; remove it and add it again.")
        self.is_custom = bool(raw.get("is_custom"))
        self.drug_id, self.dosage_id, self.frequency_id = num("drug_id"), num("dosage_id"), num("frequency_id")
        self.name = str(raw.get("name") or "")
        self.dosage_text, self.frequency_text = str(raw.get("dosage_text") or ""), str(raw.get("frequency_text") or "")
        self.eye = str(raw.get("eye") or "")
        self.indication, self.note = str(raw.get("indication") or ""), str(raw.get("note") or "")


def parse_prescribed(db: Session, raw: Optional[str]) -> list:
    """The staged JSON -> [{'values': resolve_selection output, 'indication': ..., 'note': ...}]. ValueError (a sentence meant for
    the user, naming the entry) on bad JSON or any entry the intake rules refuse; an empty or missing field means nothing prescribed."""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        raise ValueError("The prescribed medications could not be read; remove them and add them again.")
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise ValueError("The prescribed medications could not be read; remove them and add them again.")
    if len(items) > MAX_PRESCRIBED:
        raise ValueError(f"At most {MAX_PRESCRIBED} medications can be prescribed at one visit.")
    out = []
    for n, item in enumerate(items, 1):
        payload = _Payload(item)
        try:
            values = catalog.resolve_selection(db, payload)
        except ValueError as e:
            label = (payload.name or "").strip() or f"#{n}"
            raise ValueError(f"Prescribed medication {label}: {e}")
        out.append({"values": values, "indication": payload.indication, "note": payload.note})
    return out


def add_prescribed(db: Session, exam, user_id: int, items: list) -> list:
    """Add each parsed entry to the exam's patient as an active 'prescribed' medication, replacing an identical active one.
    `exam` must already be flushed (it needs its id). Returns the new PatientMedication rows."""
    created = []
    for item in items:
        v = item["values"]
        for old in (db.query(PatientMedication).filter(PatientMedication.patient_id == exam.patient_id, PatientMedication.status == "active").all()):
            if (old.name or "").strip().lower() == v["name"].strip().lower() and (old.strength or "") == (v["strength"] or "") \
                    and (old.eye or "") == (v["eye"] or ""):
                med_svc.set_medication_status(db, old, "stopped", user_id)
        m = med_svc.add_resolved_medication(db, exam.patient_id, user_id, v, item["indication"], item["note"], exam.exam_date)
        m.source, m.prescribed_exam_id = "prescribed", exam.id
        created.append(m)
    return created


def tests_to_order(db: Session, exam, codes: list) -> list:
    """The DiagnosticTest rows to order from the step: known, active catalog codes only, each once, and not any test already ordered for
    this exam from another panel. The caller runs the NCCI check on these, then creates the orders with `place_orders`."""
    codes = list(dict.fromkeys(c for c in codes if c))
    if not codes:
        return []
    already = {t for (t,) in db.query(DiagnosticOrder.diagnostic_test_id).filter(DiagnosticOrder.ordered_exam_id == exam.id).all()}
    tests = db.query(DiagnosticTest).filter(DiagnosticTest.code.in_(codes), DiagnosticTest.active.is_(True)).all()
    return [t for t in sorted(tests, key=lambda t: codes.index(t.code)) if t.id not in already]


def place_orders(db: Session, exam, user_id: int, tests: list, notes: Optional[str] = None) -> list:
    rows = [DiagnosticOrder(patient_id=exam.patient_id, diagnostic_test_id=t.id, ordered_by_user_id=user_id,
                            ordered_exam_id=exam.id, notes=(notes or "").strip()[:500] or None) for t in tests]
    db.add_all(rows)
    return rows

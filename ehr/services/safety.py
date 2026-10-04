"""Safety-flag / warning-rule logic (ROS plan stage 5).

evaluate() is the one place a rule's firing is decided, shared by the exam-form banner feed, the
exam detail page and the sign step, so they can never disagree:

    a rule fires for a patient when
        rule.is_active AND rule.reviewed
        AND the patient's flag for the rule's flag type is 'yes' -- answered 'yes' on the chart, OR derived 'yes' by
            an active medication in a reviewed class term linked to that flag (ehr/services/medications.py);
            the medication list wins over a manual 'no'
        AND (rule.keyword is blank OR the keyword appears (case-insensitive) in the exam text).

Unreviewed or inactive rules are silent. Unknown flag state (no row) never fires a rule: a missing
answer is "unknown", not "yes". Everything is decision support -- nothing here alters the note,
billing, or an order. Reads are small indexed queries; the helpers never raise into a caller.
"""
import logging
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from ehr.models.safety import (ExamSafetyAcknowledgement, PatientSafetyFlag, SafetyFlagType,
                               SafetyRule)
from ehr.services import field_audit

log = logging.getLogger(__name__)


def exam_text(exam) -> str:
    """The free text a keyword rule is matched against."""
    return " ".join(filter(None, [getattr(exam, f, None) for f in ("chief_complaint", "assessment", "plan", "ros_notes")]))


def _keyword_hit(keyword: Optional[str], text: str) -> bool:
    k = (keyword or "").strip().lower()
    return (not k) or (k in (text or "").lower())


def evaluate(db: Session, patient_id: int, text: Optional[str] = "") -> list:
    """Rules that fire for this patient given `text` (an exam's free text). text=None skips keyword
    filtering and returns every eligible rule with its keyword, for a client that matches live."""
    try:
        manual_yes = {r[0] for r in db.query(PatientSafetyFlag.flag_type_id)
                      .filter(PatientSafetyFlag.patient_id == patient_id, PatientSafetyFlag.status == "yes").all()}
        from ehr.services import medications as med_svc     # a flag the medication list derives as 'yes' counts as yes
        yes_ids = manual_yes | set(med_svc.derived_flags(db, patient_id))
        if not yes_ids:
            return []
        rows = (db.query(SafetyRule, SafetyFlagType)
                .join(SafetyFlagType, SafetyFlagType.id == SafetyRule.flag_type_id)
                .filter(SafetyRule.flag_type_id.in_(yes_ids), SafetyRule.is_active.is_(True),
                        SafetyRule.reviewed.is_(True), SafetyFlagType.is_active.is_(True))
                .order_by(SafetyRule.id).all())
    except Exception:                                  # noqa: BLE001 -- decision support must not break the page
        log.exception("Could not evaluate safety rules for patient %s", patient_id)
        return []
    return [{"id": r.id, "flag": t.label, "warning": r.warning_text, "keyword": (r.keyword or "").strip() or None,
             "citation": r.source_citation}
            for r, t in rows if text is None or _keyword_hit(r.keyword, text)]


def flags_for_patient(db: Session, patient_id: int) -> list:
    """[(flag_type, current_status_or_None, row_or_None)] for every active flag type."""
    types = db.query(SafetyFlagType).filter(SafetyFlagType.is_active.is_(True)).order_by(SafetyFlagType.sort_order, SafetyFlagType.id).all()
    rows = {r.flag_type_id: r for r in db.query(PatientSafetyFlag).filter(PatientSafetyFlag.patient_id == patient_id).all()}
    return [(t, rows[t.id].status if t.id in rows else None, rows.get(t.id)) for t in types]


def set_flag(db: Session, patient_id: int, flag_type_id: int, status: str, user_id: int, note: Optional[str] = None) -> bool:
    """Records a patient's answer ('yes' / 'no'; anything else clears it back to unknown). Audited.
    Returns True if something changed."""
    ftype = db.get(SafetyFlagType, flag_type_id)
    if ftype is None or not ftype.is_active:
        return False
    row = db.query(PatientSafetyFlag).filter(PatientSafetyFlag.patient_id == patient_id,
                                             PatientSafetyFlag.flag_type_id == flag_type_id).first()
    old = row.status if row else None
    new = status if status in ("yes", "no") else None
    if old == new:
        return False
    if new is None:
        db.delete(row)
    elif row is None:
        db.add(PatientSafetyFlag(patient_id=patient_id, flag_type_id=flag_type_id, status=new,
                                 note=(note or None), recorded_by_user_id=user_id, recorded_at=datetime.utcnow()))
    else:
        row.status, row.note, row.recorded_by_user_id, row.recorded_at = new, (note or row.note), user_id, datetime.utcnow()
    db.flush()
    field_audit.record_field_changes(db, "patient_safety_flags", patient_id, {ftype.code: old}, {ftype.code: new}, user_id)
    return True


def unacknowledged(db: Session, exam, acked_rule_ids: Iterable) -> list:
    """Fired rules for this exam that are not covered by `acked_rule_ids` (and not already acknowledged)."""
    fired = evaluate(db, exam.patient_id, exam_text(exam))
    done = {a.rule_id for a in db.query(ExamSafetyAcknowledgement).filter(ExamSafetyAcknowledgement.exam_id == exam.id).all()}
    acked = set()
    for v in acked_rule_ids:
        try:
            acked.add(int(v))
        except (TypeError, ValueError):
            pass
    return [f for f in fired if f["id"] not in done and f["id"] not in acked]


def record_acknowledgements(db: Session, exam, rule_ids: Iterable, user_id: int) -> int:
    """Stores an acknowledgement (with a snapshot of the warning text) for each currently-firing rule in rule_ids."""
    wanted = set()
    for v in rule_ids:
        try:
            wanted.add(int(v))
        except (TypeError, ValueError):
            pass
    fired = {f["id"]: f for f in evaluate(db, exam.patient_id, exam_text(exam))}
    existing = {a.rule_id for a in db.query(ExamSafetyAcknowledgement).filter(ExamSafetyAcknowledgement.exam_id == exam.id).all()}
    n = 0
    for rid in wanted:
        if rid in fired and rid not in existing:
            db.add(ExamSafetyAcknowledgement(exam_id=exam.id, rule_id=rid, warning_text_snapshot=fired[rid]["warning"][:500],
                                             acknowledged_by_user_id=user_id, acknowledged_at=datetime.utcnow()))
            n += 1
    db.flush()
    return n


def acknowledgements_for_exam(db: Session, exam_id: int) -> list:
    """[(acknowledgement, "First Last" or None)] in the order recorded."""
    from ehr.models.database import User
    rows = (db.query(ExamSafetyAcknowledgement, User)
            .outerjoin(User, User.id == ExamSafetyAcknowledgement.acknowledged_by_user_id)
            .filter(ExamSafetyAcknowledgement.exam_id == exam_id).order_by(ExamSafetyAcknowledgement.id).all())
    return [(a, (f"{u.first_name} {u.last_name}".strip() if u else None)) for a, u in rows]

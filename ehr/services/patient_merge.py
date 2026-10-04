"""Merging a duplicate patient into the patient being kept, and putting it back.

What a merge does, in one transaction:
  1. Finds every table with a foreign key to patients automatically (so a table added later is covered without edits) and
     moves the duplicate's rows onto the kept patient.
  2. Where a uniqueness rule would be broken (the kept patient already has that safety flag, primary-care link, recall...),
     the kept patient's row wins and the duplicate's row is set aside (stored in the event, restored by an undo). Rows that
     must not transfer at all -- import-batch membership (so undoing an import can't delete the kept chart) and portal
     login tokens/sessions -- are set aside the same way.
  3. Fills the kept patient's demographic fields as chosen. Free-text clinical fields can be combined so nothing is lost;
     balance_due is added together; text/email opt-ins stay on only if BOTH charts had them on.
  4. Removes the empty duplicate row and records a PatientMergeEvent holding everything needed to undo it.
Nothing is sent anywhere. The audit of the *duplicate's* old field edits stays keyed to its old id.
"""
import base64
import json
from datetime import date, datetime
from typing import Optional

from sqlalchemy import UniqueConstraint, delete, func, insert, select, update
from sqlalchemy.orm import Session

from ehr.models.database import Base, Patient
from ehr.models.patient_merge import PatientMergeEvent
from ehr.services import field_audit

# Child tables whose duplicate rows are set aside rather than moved (see module docstring).
SET_ASIDE_TABLES = {"data_import_batch_patients", "patient_portal_login_tokens", "patient_portal_sessions"}
TABLE_LABELS = {"appointments": "Appointments", "eye_exams": "Exams", "prescriptions": "Prescriptions", "problems": "Problems",
                "patient_documents": "Documents", "patient_insurance_plans": "Insurance plans", "waitlist_entries": "Waitlist entries",
                "diagnostic_orders": "Diagnostic orders", "rx_lab_orders": "Lab orders", "patient_medications": "Medications",
                "patient_allergies": "Allergies", "medication_list_reviews": "Medication list reviews", "patient_safety_flags": "Safety flags",
                "patient_recalls": "Recalls", "patient_primary_care": "Primary-care link", "pcp_communications": "PCP letters",
                "portal_access_audit_events": "Portal access history", "data_import_batch_patients": "Import-batch membership",
                "patient_portal_login_tokens": "Portal login links", "patient_portal_sessions": "Portal sessions"}
# Patient columns that are never chosen field by field.
_SKIP = {"id", "created_at", "self_registered_at", "photo_path", "balance_due", "sms_opt_in", "email_opt_in"}
TEXT_FIELDS = {"allergies", "medical_history", "ocular_history", "family_ocular_history", "social_history_notes"}
FIELD_LABELS = {"first_name": "First name", "last_name": "Last name", "preferred_name": "Preferred name", "mrn": "MRN", "date_of_birth": "Date of birth",
                "gender": "Gender", "phone": "Phone", "email": "Email", "address": "Address", "city": "City", "state": "State", "zip_code": "ZIP",
                "insurance_provider": "Insurance provider", "insurance_id": "Insurance ID", "emergency_contact_name": "Emergency contact",
                "emergency_contact_phone": "Emergency contact phone", "allergies": "Allergies (free text)", "medical_history": "Medical history",
                "ocular_history": "Ocular history", "family_ocular_history": "Family ocular history", "tobacco_use_status": "Tobacco use",
                "alcohol_use_status": "Alcohol use", "social_history_notes": "Social history notes"}
JOINER = "\n— from the merged record —\n"


class MergeError(ValueError):
    """The merge can't proceed (shown to the admin as-is)."""


# ----------------------------------------------------------------------------------------- JSON for the event
def _enc(v):
    if isinstance(v, datetime):
        return {"__dt": v.isoformat()}
    if isinstance(v, date):
        return {"__d": v.isoformat()}
    if isinstance(v, (bytes, bytearray)):
        return {"__b": base64.b64encode(bytes(v)).decode()}
    return v


def _dec(v):
    if isinstance(v, dict):
        if "__dt" in v:
            return datetime.fromisoformat(v["__dt"])
        if "__d" in v:
            return date.fromisoformat(v["__d"])
        if "__b" in v:
            return base64.b64decode(v["__b"])
    return v


def _row_dict(row) -> dict:
    return {k: _enc(v) for k, v in row._mapping.items()}


# ----------------------------------------------------------------------------------------- structure
def linked_tables() -> list:
    """[(table, fk column, primary-key column)] for every table with a foreign key to patients, found from the metadata."""
    out = []
    for t in Base.metadata.sorted_tables:
        if t.name == "patients":
            continue
        for fk in t.foreign_keys:
            if fk.column.table.name == "patients":
                pk = list(t.primary_key.columns)
                if len(pk) != 1:
                    raise MergeError(f"Table {t.name} has a composite key and can't be merged automatically.")
                out.append((t, fk.parent, pk[0]))
    return out


def _unique_groups(table, fkcol) -> list:
    """Other columns that, together with the patient column, must be unique; [] means the patient column alone is unique."""
    groups = []
    for c in table.constraints:
        if isinstance(c, UniqueConstraint) and fkcol.name in {x.name for x in c.columns}:
            groups.append([x for x in c.columns if x.name != fkcol.name])
    for ix in table.indexes:
        if ix.unique and fkcol.name in {x.name for x in ix.columns}:
            groups.append([x for x in ix.columns if x.name != fkcol.name])
    if fkcol.unique:
        groups.append([])
    return groups


def _aside_ids(db: Session, t, fkcol, pk, keep_id: int, dup_id: int, ids: list) -> set:
    """Ids (among the duplicate's `ids` in table t) that must be set aside instead of moved."""
    if t.name in SET_ASIDE_TABLES:
        return set(ids)
    aside = set()
    for others in _unique_groups(t, fkcol):
        if not others:
            if db.execute(select(func.count()).select_from(t).where(fkcol == keep_id)).scalar():
                return set(ids)
            continue
        have = {tuple(r) for r in db.execute(select(*others).where(fkcol == keep_id)).all()}
        for r in db.execute(select(pk, *others).where(fkcol == dup_id)).all():
            if tuple(r[1:]) in have:
                aside.add(r[0])
    return aside


def preview(db: Session, keep_id: int, dup_id: int) -> list:
    """[{table, label, keep, dup, aside}] for the compare page: how many rows each chart has, and how many of the
    duplicate's would be set aside (the kept chart already has an equivalent, or the row doesn't transfer)."""
    out = []
    for t, fkcol, pk in linked_tables():
        k = db.execute(select(func.count()).select_from(t).where(fkcol == keep_id)).scalar()
        ids = [r[0] for r in db.execute(select(pk).where(fkcol == dup_id)).all()]
        if not k and not ids:
            continue
        out.append({"table": t.name, "label": TABLE_LABELS.get(t.name, t.name.replace("_", " ").capitalize()), "keep": k, "dup": len(ids),
                    "aside": len(_aside_ids(db, t, fkcol, pk, keep_id, dup_id, ids)) if ids else 0})
    return out


def counts(db: Session, patient_id: int) -> dict:
    """{table name: row count} of the records linked to a patient (non-zero only)."""
    out = {}
    for t, fkcol, pk in linked_tables():
        n = db.execute(select(func.count()).select_from(t).where(fkcol == patient_id)).scalar()
        if n:
            out[t.name] = n
    return out


def duplicate_candidates(db: Session, limit: int = 60) -> list:
    """Likely duplicate groups: same last name + first name + date of birth (case-insensitive); same last name + date of birth
    with first names that start alike; then same date of birth + same phone digits. [{'reason', 'patients': [Patient, ...]}], largest evidence first."""
    pts = db.query(Patient).order_by(Patient.id).all()
    seen, groups = set(), []
    by_name = {}
    for p in pts:
        key = ((p.last_name or "").strip().lower(), (p.first_name or "").strip().lower(), (p.date_of_birth or "")[:10])
        if key[0] and key[1] and key[2]:
            by_name.setdefault(key, []).append(p)
    for ps in by_name.values():
        if len(ps) > 1:
            groups.append({"reason": "Same name and date of birth", "patients": ps})
            seen.update(p.id for p in ps)
    by_last_dob = {}
    for p in pts:
        key = ((p.last_name or "").strip().lower(), (p.date_of_birth or "")[:10])
        if key[0] and key[1]:
            by_last_dob.setdefault(key, []).append(p)
    for ps in by_last_dob.values():
        # same last name and birth date, first names that look alike (Pat / Patricia): a nickname or a short form
        like = [p for p in ps if any(q.id != p.id and (q.first_name or "").strip().lower()[:3] == (p.first_name or "").strip().lower()[:3]
                                     and (p.first_name or "").strip() for q in ps)]
        if len(like) > 1 and not all(p.id in seen for p in like):
            groups.append({"reason": "Similar first name, same last name and date of birth", "patients": like})
            seen.update(p.id for p in like)
    by_phone = {}
    for p in pts:
        digits = "".join(ch for ch in (p.phone or "") if ch.isdigit())[-10:]
        if len(digits) == 10 and (p.date_of_birth or "")[:10]:
            by_phone.setdefault((digits, p.date_of_birth[:10]), []).append(p)
    for ps in by_phone.values():
        ps = [p for p in ps]
        if len(ps) > 1 and not all(p.id in seen for p in ps):
            groups.append({"reason": "Same phone and date of birth", "patients": ps})
    return groups[:limit]


# ----------------------------------------------------------------------------------------- field plan
def field_plan(keep: Patient, dup: Patient) -> list:
    """One row per choosable demographic field: values from both charts and the default choice
    ('keep' the kept chart's value, 'dup' take the duplicate's, 'both' combine free text)."""
    rows = []
    for col in Patient.__table__.columns:
        name = col.name
        if name in _SKIP or name not in FIELD_LABELS:
            continue
        a, b = getattr(keep, name), getattr(dup, name)
        same = (a or "") == (b or "")
        if same or not b:
            default = "keep"
        elif not a:
            default = "dup"
        else:
            default = "both" if name in TEXT_FIELDS else "keep"
        rows.append({"name": name, "label": FIELD_LABELS[name], "keep": a, "dup": b, "same": same, "default": default,
                     "can_combine": name in TEXT_FIELDS})
    return rows


def _choose(name: str, choice: str, a, b):
    if choice == "dup":
        return b
    if choice == "both" and name in TEXT_FIELDS:
        if a and b and a.strip() != b.strip():
            return f"{a}{JOINER}{b}"
        return a or b
    return a


def _chk_patients(db, keep_id: int, dup_id: int):
    if keep_id == dup_id:
        raise MergeError("Choose two different patients.")
    keep, dup = db.get(Patient, keep_id), db.get(Patient, dup_id)
    if not keep or not dup:
        raise MergeError("One of those patients no longer exists.")
    return keep, dup


# ----------------------------------------------------------------------------------------- merge
def merge(db: Session, keep_id: int, dup_id: int, user_id: int, reason: str, choices: Optional[dict] = None) -> PatientMergeEvent:
    """Merges patient `dup_id` into `keep_id` inside the caller's transaction (the caller commits). Raises MergeError."""
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise MergeError("Give a reason for the merge (at least a few words).")
    keep, dup = _chk_patients(db, keep_id, dup_id)
    choices = choices or {}
    cols = [c.name for c in Patient.__table__.columns]
    dup_snapshot = {c: _enc(getattr(dup, c)) for c in cols}

    # 1. Demographic fields onto the kept patient.
    before, changed = {}, {}
    for row in field_plan(keep, dup):
        new = _choose(row["name"], choices.get(row["name"], row["default"]), row["keep"], row["dup"])
        if (new or None) != (row["keep"] or None):
            before[row["name"]], changed[row["name"]] = row["keep"], new
    extra = {}
    if (dup.balance_due or 0) != 0:
        extra["balance_due"] = (keep.balance_due or 0) + dup.balance_due
    if keep.photo_path is None and dup.photo_path:
        extra["photo_path"] = dup.photo_path
    for flag in ("sms_opt_in", "email_opt_in"):
        if getattr(keep, flag) and not getattr(dup, flag):
            extra[flag] = False                                       # opted in only if BOTH charts had opted in
    for k, v in extra.items():
        before[k], changed[k] = getattr(keep, k), v
    if "mrn" in changed and changed["mrn"] and changed["mrn"] == dup.mrn:
        dup.mrn = None                                               # the MRN is unique: free it before the kept chart takes it
        db.flush()
    for k, v in changed.items():
        setattr(keep, k, v)
    field_audit.record_field_changes(db, "patients", keep.id, before, {k: changed[k] for k in changed}, user_id)
    db.flush()

    # 2. Move linked rows, set aside the ones that can't transfer.
    moved, dropped = {}, {}
    for t, fkcol, pk in linked_tables():
        ids = [r[0] for r in db.execute(select(pk).where(fkcol == dup_id)).all()]
        if not ids:
            continue
        aside = _aside_ids(db, t, fkcol, pk, keep_id, dup_id, ids)
        if aside:
            rows = db.execute(select(t).where(pk.in_(list(aside)))).all()
            dropped[t.name] = {"pk": pk.name, "rows": [_row_dict(r) for r in rows]}
            db.execute(delete(t).where(pk.in_(list(aside))))
        go = [i for i in ids if i not in aside]
        for i in range(0, len(go), 500):
            db.execute(update(t).where(pk.in_(go[i:i + 500])).values({fkcol.name: keep_id}))
        if go:
            moved[t.name] = {"col": fkcol.name, "pk": pk.name, "ids": go}
    leftover = [t.name for t, fkcol, _ in linked_tables() if db.execute(select(func.count()).select_from(t).where(fkcol == dup_id)).scalar()]
    if leftover:
        raise MergeError("Some records could not be moved (" + ", ".join(leftover) + "); nothing was changed.")

    # 3. Remove the empty duplicate and record the event.
    label = f"{dup.last_name}, {dup.first_name}"[:200]
    db.expunge(dup)
    db.execute(delete(Patient.__table__).where(Patient.__table__.c.id == dup_id))
    ev = PatientMergeEvent(survivor_id=keep_id, merged_patient_id=dup_id, merged_label=label, status="merged", reason=reason[:500],
                           moved_total=sum(len(v["ids"]) for v in moved.values()), dropped_total=sum(len(v["rows"]) for v in dropped.values()),
                           details_json=json.dumps({"dup": dup_snapshot, "before": {k: _enc(v) for k, v in before.items()}, "moved": moved, "dropped": dropped}),
                           merged_by_user_id=user_id)
    db.add(ev)
    db.flush()
    field_audit.record_field_changes(db, "patient_merge_events", ev.id, {}, {"survivor_id": keep_id, "merged_patient_id": dup_id, "status": "merged"}, user_id)
    return ev


# ----------------------------------------------------------------------------------------- undo
def undo(db: Session, event: PatientMergeEvent, user_id: int) -> dict:
    """Puts a merge back: recreates the duplicate with its own id and fields, restores the kept patient's fields, moves each
    row back that is still where the merge left it, and restores the rows that were set aside. Rows that have since been
    deleted or moved elsewhere are skipped and counted. Raises MergeError if it can't be undone."""
    if event.status != "merged":
        raise MergeError("That merge has already been undone.")
    keep = db.get(Patient, event.survivor_id)
    if not keep:
        raise MergeError("The kept patient no longer exists (it may have been merged into another chart), so this merge can't be undone.")
    if db.get(Patient, event.merged_patient_id):
        raise MergeError("A patient with the original id exists again, so this merge can't be undone.")
    d = json.loads(event.details_json or "{}")
    tables = {t.name: (t, fkcol, pk) for t, fkcol, pk in linked_tables()}

    # Kept patient's fields first (frees a taken MRN), then the duplicate row.
    for k, v in d.get("before", {}).items():
        setattr(keep, k, _dec(v))
    db.flush()
    snap = {k: _dec(v) for k, v in d["dup"].items()}
    db.execute(insert(Patient.__table__).values(snap))

    restored = skipped = 0
    for name, info in d.get("moved", {}).items():
        if name not in tables:
            skipped += len(info["ids"])
            continue
        t, fkcol, pk = tables[name]
        for i in range(0, len(info["ids"]), 500):
            chunk = info["ids"][i:i + 500]
            res = db.execute(update(t).where(pk.in_(chunk), fkcol == event.survivor_id).values({fkcol.name: event.merged_patient_id}))
            restored += res.rowcount or 0
            skipped += len(chunk) - (res.rowcount or 0)
    for name, info in d.get("dropped", {}).items():
        if name not in tables:
            skipped += len(info["rows"])
            continue
        t, _, pk = tables[name]
        for row in info["rows"]:
            vals = {k: _dec(v) for k, v in row.items()}
            if db.execute(select(func.count()).select_from(t).where(pk == vals[pk.name])).scalar():
                skipped += 1
                continue
            db.execute(insert(t).values(vals))
            restored += 1
    event.status, event.undone_by_user_id, event.undone_at = "undone", user_id, datetime.utcnow()
    field_audit.record_field_changes(db, "patient_merge_events", event.id, {"status": "merged"}, {"status": "undone"}, user_id)
    db.flush()
    return {"restored": restored, "skipped": skipped}

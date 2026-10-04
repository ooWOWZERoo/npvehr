"""Medication-list import (spreadsheet -> existing patients' medication lists). Same stage -> preview -> attest -> import
-> undo shape as the recall import and reuses its file reader and the data_import_batches table.

One row per medication. Columns are found by header name (spelling variants accepted, order irrelevant):
  LastName*, FirstName*, DOB*   identify an EXISTING patient (same rule as the recall import: last name, first name and date
                                of birth match, case-insensitive). This import never creates or changes a patient.
  Medication*                   the drug as written, e.g. "Latanoprost 0.005%"
  Strength, Route, Eye (OD/OS/OU), Frequency, For (indication), Start Date, Status (active/stopped), Note
Rules:
  * A row whose patient isn't found, or matches more than one patient, is skipped (counted by reason, never listed by name).
  * Imported medications are marked source "imported" and are NOT a review: a list that was never reviewed stays "not yet
    reviewed" (and a reviewed one is not marked reviewed) until a clinician confirms it on the chart or the exam form.
  * Existing medications are never changed. A row matching one already on the patient's list (same name, strength and eye,
    case-insensitive) is skipped, so running a file twice adds nothing the second time.
  * Undo removes the medications a batch added unless they have since been edited.
Nothing here logs or stores patient identifiers in summaries or warnings.
"""
import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from sqlalchemy.orm import Session

from ehr.models.database import Patient
from ehr.models.imports import DataImportBatch, DataImportBatchRecord
from ehr.models.medications import MedicationClass, MedicationClassTerm, PatientMedication
from ehr.services import field_audit
from ehr.services import medications as med_svc
from ehr.services.recall_import import ImportFileError, _raw_grid, parse_date

KIND = "medication_list"
TEMPLATE_COLUMNS = ["LastName", "FirstName", "DOB", "Medication", "Strength", "Route", "Eye", "Frequency", "For", "Start Date", "Status", "Note"]
_HEADERS = {"lastname": "last", "last": "last", "firstname": "first", "first": "first", "dob": "dob", "dateofbirth": "dob", "birthdate": "dob",
            "medication": "name", "medicationname": "name", "drug": "name", "drugname": "name",
            "strength": "strength", "dose": "strength", "route": "route", "eye": "eye", "laterality": "eye",
            "frequency": "frequency", "sig": "frequency", "for": "indication", "indication": "indication", "reason": "indication",
            "startdate": "start", "started": "start", "status": "status", "note": "note", "notes": "note", "comment": "note"}
_STOPPED = {"stopped", "discontinued", "inactive", "d/c", "dc", "past", "former"}
_ACTIVE = {"", "active", "current", "ongoing", "taking"}


def _hkey(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def read_rows(file_name: str, data: bytes) -> list:
    grid = _raw_grid(file_name, data)
    header_at, colmap = None, {}
    for i, row in enumerate(grid[:10]):
        cm = {j: _HEADERS[_hkey(c)] for j, c in enumerate(row) if _hkey(c) in _HEADERS}
        if {"last", "first", "dob", "name"} <= set(cm.values()):
            header_at, colmap = i, cm
            break
    if header_at is None:
        raise ImportFileError("Couldn't find the header row. Expected columns include LastName, FirstName, DOB and Medication "
                              "(download the template for the full layout).")
    rows = []
    for i in range(header_at + 1, len(grid)):
        row = grid[i]
        if not any(c.strip() for c in row):
            continue
        rec = {n: "" for n in set(_HEADERS.values())}
        for j, name in colmap.items():
            if j < len(row):
                rec[name] = row[j].strip()
        rec["row"] = i + 1
        rows.append(rec)
    return rows


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


@dataclass
class MedDraft:
    patient_id: int
    name: str
    strength: Optional[str]
    route: Optional[str]
    eye: Optional[str]
    frequency: Optional[str]
    indication: Optional[str]
    start_date: Optional[object]
    status: str
    note: Optional[str]


@dataclass
class Plan:
    rows_total: int = 0
    skipped: list = field(default_factory=list)            # [(row number, reason)]
    drafts: list = field(default_factory=list)
    patients_matched: set = field(default_factory=set)
    already_on_file: int = 0
    repeats_in_file: int = 0
    warnings: Counter = field(default_factory=Counter)
    unclassified: int = 0

    @property
    def summary_lines(self) -> list:
        w, out = self.warnings, []
        if self.already_on_file:
            out.append(f"{self.already_on_file} medication(s) already on a patient's list were skipped.")
        if self.repeats_in_file:
            out.append(f"{self.repeats_in_file} repeated row(s) in the file were merged.")
        if w["bad_eye"]:
            out.append(f"{w['bad_eye']} row(s) had an Eye value other than OD, OS or OU; the eye was left blank.")
        if w["bad_date"]:
            out.append(f"{w['bad_date']} row(s) had a start date that couldn't be read; it was left blank.")
        if w["bad_status"]:
            out.append(f"{w['bad_status']} row(s) had an unrecognised Status; they were imported as active.")
        if self.unclassified:
            out.append(f"{self.unclassified} medication(s) match no reviewed drug class, so they can't trigger a safety warning (add and sign off the class names in Medication Classes).")
        if self.drafts:
            out.append("Imported medications do not count as a review: each patient's list still needs a clinician to review it.")
        return out


def build_plan(db: Session, rows: list) -> Plan:
    plan = Plan(rows_total=len(rows))
    index = {}
    for pid, last, first, dob in db.query(Patient.id, Patient.last_name, Patient.first_name, Patient.date_of_birth).all():
        index.setdefault((_norm(last), _norm(first), (dob or "")[:10]), []).append(pid)
    have = {}
    seen = set()
    for r in rows:
        name = " ".join((r["name"] or "").split())[:160]
        if not name:
            plan.skipped.append((r["row"], "No medication name"))
            continue
        dob = parse_date(r["dob"])
        if not (r["last"] and r["first"]) or not dob:
            plan.skipped.append((r["row"], "Missing name or unreadable date of birth"))
            continue
        ids = index.get((_norm(r["last"]), _norm(r["first"]), dob.isoformat()), [])
        if not ids:
            plan.skipped.append((r["row"], "No matching patient in the system"))
            continue
        if len(ids) > 1:
            plan.skipped.append((r["row"], "More than one matching patient"))
            continue
        pid = ids[0]
        eye = r["eye"].strip().upper()
        if eye and eye not in med_svc.EYES:
            plan.warnings["bad_eye"] += 1
            eye = ""
        start = parse_date(r["start"]) if r["start"] else None
        if r["start"] and not start:
            plan.warnings["bad_date"] += 1
        st = r["status"].strip().lower()
        if st in _STOPPED:
            status = "stopped"
        else:
            status = "active"
            if st not in _ACTIVE:
                plan.warnings["bad_status"] += 1
        strength = r["strength"][:80] or None
        key = (pid, _norm(name), _norm(strength or ""), eye)
        if pid not in have:
            have[pid] = {(_norm(m.name), _norm(m.strength or ""), m.eye or "") for m in
                         db.query(PatientMedication).filter(PatientMedication.patient_id == pid).all()}
        if key[1:] in have[pid]:
            plan.already_on_file += 1
            continue
        if key in seen:
            plan.repeats_in_file += 1
            continue
        seen.add(key)
        plan.patients_matched.add(pid)
        plan.drafts.append(MedDraft(pid, name, strength, r["route"][:40] or None, eye or None, r["frequency"][:80] or None,
                                    r["indication"][:160] or None, start, status, r["note"][:255] or None))
    terms = [t for (t,) in db.query(MedicationClassTerm.term).join(MedicationClass, MedicationClass.id == MedicationClassTerm.class_id)
             .filter(MedicationClassTerm.reviewed.is_(True), MedicationClass.is_active.is_(True)).all()]
    plan.unclassified = sum(1 for d in plan.drafts if d.status == "active" and not any(med_svc.term_matches(t, d.name) for t in terms))
    return plan


def apply_plan(db: Session, plan: Plan, batch: DataImportBatch, user_id: int) -> int:
    """Adds the planned medications inside the caller's transaction. Returns how many were created."""
    rows = [PatientMedication(patient_id=d.patient_id, name=d.name, strength=d.strength, route=d.route, eye=d.eye, frequency=d.frequency,
                              indication=d.indication, start_date=d.start_date, status=d.status,
                              stop_date=None, source="imported", note=d.note, recorded_by_user_id=user_id) for d in plan.drafts]
    db.add_all(rows)
    db.flush()
    for m in rows:
        field_audit.record_field_changes(db, "patient_medications", m.id, {}, {"name": m.name, "status": m.status}, user_id)
    db.add_all([DataImportBatchRecord(batch_id=batch.id, record_table="patient_medications", record_id=m.id) for m in rows])
    batch.rows_total, batch.rows_skipped = plan.rows_total, len(plan.skipped)
    batch.recalls_created, batch.patients_matched = len(rows), len(plan.patients_matched)     # columns reused: medications / patients
    db.flush()
    return len(rows)


def undo_batch(db: Session, batch: DataImportBatch, user_id: int) -> tuple:
    """Removes the medications this batch added unless they were edited afterwards (updated more than a couple of seconds
    after being recorded). Returns (removed, kept)."""
    ids = [r.record_id for r in db.query(DataImportBatchRecord).filter(DataImportBatchRecord.batch_id == batch.id,
                                                                       DataImportBatchRecord.record_table == "patient_medications").all()]
    removed = kept = 0
    for m in db.query(PatientMedication).filter(PatientMedication.id.in_(ids or [0])).all():
        if m.updated_at and m.recorded_at and m.updated_at - m.recorded_at > timedelta(seconds=2):
            kept += 1
            continue
        field_audit.record_field_changes(db, "patient_medications", m.id, {"name": m.name}, {"name": None}, user_id)
        db.delete(m)
        removed += 1
    db.query(DataImportBatchRecord).filter(DataImportBatchRecord.batch_id == batch.id).delete(synchronize_session=False)
    batch.status, batch.patients_undone, batch.patients_kept_on_undo = "undone", removed, kept     # columns reused: removed / kept
    return removed, kept


def template_csv() -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(TEMPLATE_COLUMNS)
    w.writerow(["Sample", "Pat", "1970-01-31", "Latanoprost 0.005%", "0.005%", "ophthalmic", "OU", "once nightly", "glaucoma", "2023-05-01", "active", "Example only: use a real patient's details"])
    w.writerow(["Sample", "Pat", "1970-01-31", "Lisinopril", "10 mg", "oral", "", "daily", "", "", "active", ""])
    return buf.getvalue()

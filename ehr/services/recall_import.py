"""Recall-report import (patients + recalls) -- parsing, normalising, planning, applying and undoing.

The report is an export from a practice-management system: a header row, then repeated "Recall Type: <label>"
section rows each followed by that group's patients (columns: Scheduled Recall Date, Next Appt Date, Last Exam
Date, LastName, FirstName, DOB, Sex, Phone#, Address 1, Address 2, City, Zip, State, Email). Accepts the original
.xls or a .csv saved from it (columns are found by header name, so order doesn't matter).

How each column is mapped (see also spec §113):
  LastName / FirstName  -> Patient.last_name / first_name, tidied (ALL-CAPS or all-lowercase becomes Title Case)
  DOB                   -> Patient.date_of_birth as YYYY-MM-DD (the app's format)
  Sex                   -> Patient.gender: F -> 'F', M -> 'M'; anything else (e.g. 'U') -> left blank
  Phone#                -> Patient.phone as (610) 555-0100; odd lengths kept as typed and counted as a warning
  Address 1 / Address 2 -> Patient.address ("line 1, line 2")
  City / State / Zip    -> Patient.city / state / zip_code (zip kept as typed; odd shapes counted as a warning)
  Email                 -> Patient.email, lower-cased; invalid ones dropped and counted
  Recall Type section   -> PatientRecall.recall_type (kept as labelled) + interval_months / audience parsed from it
  Scheduled Recall Date -> PatientRecall.due_date
  Last Exam Date        -> PatientRecall.last_exam_date ("Never" -> never_examined)
  Next Appt Date        -> PatientRecall.next_appt_date
Deliberately NOT done: no exams or appointments are created from the last-exam / next-appt dates (no clinical or
scheduling content is invented), and reminder opt-ins stay OFF, so nobody is contacted because of an import.

A patient is the same person when last name, first name and date of birth match (case-insensitive), whether the
match is another row in the file or a patient already in the system. Matches are never overwritten; the import
only adds new patients and recall rows, so running a file twice adds nothing the second time.
Nothing in this module logs or stores patient identifiers in its summaries or warnings.
"""
import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ehr.models.database import Base, Patient
from ehr.models.imports import DataImportBatch, DataImportBatchPatient, PatientRecall, RecallAction

MAX_FILE_BYTES = 4_000_000          # stays under Vercel's request-body limit
HEADERS = {"scheduled recall date": "recall_date", "next appt date": "next_appt", "last exam date": "last_exam",
           "lastname": "last", "firstname": "first", "dob": "dob", "sex": "sex", "phone#": "phone", "phone": "phone",
           "address 1": "addr1", "address 2": "addr2", "city": "city", "zip": "zip", "state": "state", "email": "email"}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")


class ImportFileError(ValueError):
    """The file can't be read as a recall report (shown to the admin as-is)."""


# ----------------------------------------------------------------------------------------- reading
def _cell_text(sheet, r, c, datemode, xlrd) -> str:
    t = sheet.cell_type(r, c)
    v = sheet.cell_value(r, c)
    if t == xlrd.XL_CELL_DATE:
        try:
            return xlrd.xldate_as_datetime(v, datemode).strftime("%m/%d/%Y")
        except Exception:                                   # noqa: BLE001
            return str(v)
    if t == xlrd.XL_CELL_NUMBER:
        return str(int(v)) if float(v).is_integer() else str(v)
    return str(v).strip()


def _raw_grid(file_name: str, data: bytes) -> list:
    name = (file_name or "").lower()
    if name.endswith(".xlsx"):
        raise ImportFileError("This is an .xlsx file. Save it as .xls or .csv and upload that instead.")
    if name.endswith(".csv"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text))]
    if name.endswith(".xls"):
        try:
            import xlrd
        except ImportError as exc:                          # pragma: no cover
            raise ImportFileError("Reading .xls files needs the 'xlrd' package; upload a .csv instead.") from exc
        try:
            wb = xlrd.open_workbook(file_contents=data)
        except Exception as exc:                            # noqa: BLE001
            raise ImportFileError("That file could not be read as an Excel .xls workbook.") from exc
        sh = wb.sheet_by_index(0)
        return [[_cell_text(sh, r, c, wb.datemode, xlrd) for c in range(sh.ncols)] for r in range(sh.nrows)]
    raise ImportFileError("Upload the report as an .xls or .csv file.")


def read_rows(file_name: str, data: bytes) -> list:
    """Raw row dicts (text values) with their 1-based source row number and the recall type of their section."""
    grid = _raw_grid(file_name, data)
    header_at, colmap = None, {}
    for i, row in enumerate(grid[:15]):
        cm = {j: HEADERS[c.strip().lower()] for j, c in enumerate(row) if c.strip().lower() in HEADERS}
        if {"last", "first", "dob"} <= set(cm.values()):
            header_at, colmap = i, cm
            break
    if header_at is None:
        raise ImportFileError("Couldn't find the header row. Expected columns include LastName, FirstName and DOB.")
    rows, recall_type = [], None
    for i in range(header_at + 1, len(grid)):
        row = grid[i]
        if not any(c.strip() for c in row):
            continue
        if row and row[0].strip().lower().startswith("recall type"):
            recall_type = next((c.strip() for c in row[1:] if c.strip()), None)
            continue
        rec = {name: "" for name in set(HEADERS.values())}
        for j, name in colmap.items():
            if j < len(row):
                rec[name] = row[j].strip()
        rec["row"] = i + 1
        rec["recall_type"] = recall_type
        rows.append(rec)
    return rows


# ----------------------------------------------------------------------------------------- normalising
def title_name(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    if not s or not (s.isupper() or s.islower()):
        return s                                             # mixed case was typed deliberately; keep it
    out = s.title()
    out = re.sub(r"\bMc([a-z])", lambda m: "Mc" + m.group(1).upper(), out)
    out = re.sub(r"\bO'([a-z])", lambda m: "O'" + m.group(1).upper(), out)
    return out


def parse_date(s) -> Optional[date]:
    s = (s or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def norm_phone(s: str):
    """(value, ok). 10 digits -> (610) 555-0100; anything else is kept as typed and flagged."""
    raw = (s or "").strip()
    if not raw:
        return None, True
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}", True
    return raw, False


def norm_email(s: str):
    """(value, ok). Blank is fine; an invalid address is dropped."""
    e = (s or "").strip().lower()
    if not e:
        return None, True
    return (e, True) if _EMAIL_RE.match(e) else (None, False)


def parse_recall_type(label: Optional[str]):
    label = re.sub(r"\s+", " ", (label or "").strip())
    m = re.search(r"(\d+)\s*month", label, re.I)
    low = label.lower()
    audience = "child" if "child" in low else ("adult" if "adult" in low else None)
    return (label or "Unspecified")[:80], (int(m.group(1)) if m else None), audience


# ----------------------------------------------------------------------------------------- planning
@dataclass
class PatientDraft:
    first_row: int
    fields: dict
    conflicts: bool = False


@dataclass
class RecallDraft:
    key: tuple
    recall_type: str
    interval_months: Optional[int]
    audience: Optional[str]
    due: date
    last_exam: Optional[date]
    never: bool
    next_appt: Optional[date]


@dataclass
class Plan:
    rows_total: int = 0
    skipped: list = field(default_factory=list)           # [(row number, reason)]
    new: dict = field(default_factory=dict)               # key -> PatientDraft
    matched: dict = field(default_factory=dict)           # key -> existing patient id
    recalls: list = field(default_factory=list)           # [RecallDraft] not already on file
    recalls_already_on_file: int = 0
    warnings: Counter = field(default_factory=Counter)

    @property
    def summary_lines(self) -> list:
        w, out = self.warnings, []
        if w["merged_rows"]:
            out.append(f"{w['merged_rows']} rows repeated a patient already in the file (same name and date of birth); they were merged into one patient, each keeping its own recall.")
        if w["conflicting_contact"]:
            out.append(f"{w['conflicting_contact']} repeated patients had differing contact details; the first row's details were kept.")
        if w["odd_phone"]:
            out.append(f"{w['odd_phone']} phone numbers weren't 10 digits and were kept as typed.")
        if w["odd_zip"]:
            out.append(f"{w['odd_zip']} ZIP codes had an unusual shape and were kept as typed.")
        if w["invalid_email"]:
            out.append(f"{w['invalid_email']} email addresses were invalid and were left blank.")
        if w["unknown_sex"]:
            out.append(f"{w['unknown_sex']} rows had no usable sex value; gender was left blank.")
        if w["bad_recall_date"]:
            out.append(f"{w['bad_recall_date']} rows had no readable scheduled recall date, so no recall was created for them (the patient still was).")
        return out


def _key(last: str, first: str, dob: date) -> tuple:
    return (last.strip().lower(), first.strip().lower(), dob.isoformat())


def build_plan(db: Session, rows: list) -> Plan:
    plan = Plan(rows_total=len(rows))
    existing = {}
    for pid, last, first, dob in db.query(Patient.id, Patient.last_name, Patient.first_name, Patient.date_of_birth).all():
        d = parse_date(dob)
        if d and last and first:
            existing.setdefault(_key(last, first, d), pid)
    seen_recalls, seen_keys = set(), set()
    for r in rows:
        last, first = title_name(r["last"]), title_name(r["first"])
        dob = parse_date(r["dob"])
        if not last or not first:
            plan.skipped.append((r["row"], "missing first or last name")); continue
        if dob is None:
            plan.skipped.append((r["row"], "unreadable date of birth")); continue
        if dob > date.today() or dob.year < 1900:
            plan.skipped.append((r["row"], "date of birth out of range")); continue
        key = _key(last, first, dob)
        phone, phone_ok = norm_phone(r["phone"])
        email, email_ok = norm_email(r["email"])
        zip_code = r["zip"].strip() or None
        sex = r["sex"].strip().upper()
        gender = sex if sex in ("F", "M") else None
        a1, a2 = r["addr1"].strip(), r["addr2"].strip()
        fields = {"first_name": first, "last_name": last, "date_of_birth": dob.isoformat(), "gender": gender,
                  "phone": phone, "email": email, "address": ", ".join(x for x in (a1, a2) if x) or None,
                  "city": title_name(r["city"]) or None, "state": r["state"].strip().upper()[:2] or None, "zip_code": zip_code}
        if key in seen_keys:
            plan.warnings["merged_rows"] += 1
            if key in plan.new:
                draft = plan.new[key]
                for k, v in fields.items():
                    if draft.fields.get(k) in (None, "") and v not in (None, ""):
                        draft.fields[k] = v
                    elif v not in (None, "") and draft.fields.get(k) not in (None, "", v) and k in ("phone", "email", "address", "city", "zip_code"):
                        draft.conflicts = True
        else:
            seen_keys.add(key)
            if not phone_ok:
                plan.warnings["odd_phone"] += 1
            if not email_ok:
                plan.warnings["invalid_email"] += 1
            if zip_code and not _ZIP_RE.match(zip_code):
                plan.warnings["odd_zip"] += 1
            if gender is None:
                plan.warnings["unknown_sex"] += 1
            if key in existing:
                plan.matched[key] = existing[key]
            else:
                plan.new[key] = PatientDraft(first_row=r["row"], fields=fields)
        due = parse_date(r["recall_date"])
        if due is None:
            plan.warnings["bad_recall_date"] += 1
            continue
        rtype, months, audience = parse_recall_type(r["recall_type"])
        last_text = r["last_exam"].strip()
        never = last_text.lower() == "never"
        rk = (key, rtype, due)
        if rk in seen_recalls:
            continue
        seen_recalls.add(rk)
        plan.recalls.append(RecallDraft(key=key, recall_type=rtype, interval_months=months, audience=audience, due=due,
                                        last_exam=None if never else parse_date(last_text), never=never,
                                        next_appt=parse_date(r["next_appt"])))
    plan.warnings["conflicting_contact"] = sum(1 for d in plan.new.values() if d.conflicts)
    # Drop recalls that are already on file for patients we matched; count them instead.
    if plan.matched:
        have = {(pid, t, d) for pid, t, d in db.query(PatientRecall.patient_id, PatientRecall.recall_type, PatientRecall.due_date)
                .filter(PatientRecall.patient_id.in_(set(plan.matched.values()))).all()}
        kept = []
        for rd in plan.recalls:
            if rd.key in plan.matched and (plan.matched[rd.key], rd.recall_type, rd.due) in have:
                plan.recalls_already_on_file += 1
            else:
                kept.append(rd)
        plan.recalls = kept
    return plan


# ----------------------------------------------------------------------------------------- applying / undoing
def apply_plan(db: Session, plan: Plan, batch: DataImportBatch) -> None:
    """Creates the planned patients and recalls inside the caller's transaction (the caller commits)."""
    key_to_id = dict(plan.matched)
    drafts = list(plan.new.items())
    patients = [Patient(sms_opt_in=False, email_opt_in=False, **d.fields) for _, d in drafts]
    db.add_all(patients)
    db.flush()
    for (key, _), p in zip(drafts, patients):
        key_to_id[key] = p.id
    db.add_all([DataImportBatchPatient(batch_id=batch.id, patient_id=p.id) for p in patients])
    db.add_all([PatientRecall(patient_id=key_to_id[rd.key], recall_type=rd.recall_type, interval_months=rd.interval_months,
                              audience=rd.audience, due_date=rd.due, last_exam_date=rd.last_exam, never_examined=rd.never,
                              next_appt_date=rd.next_appt, import_batch_id=batch.id) for rd in plan.recalls])
    db.flush()
    batch.rows_total, batch.rows_skipped = plan.rows_total, len(plan.skipped)
    batch.patients_created, batch.patients_matched = len(patients), len(plan.matched)
    batch.recalls_created = len(plan.recalls)


_OWN_TABLES = {"patient_recalls", "data_import_batch_patients", "patient_safety_flags"}


def _patients_with_other_records(db: Session, patient_ids: list) -> set:
    """Ids among patient_ids referenced by any table other than the import's own (appointments, exams, Rx, charges, ...)."""
    held = set()
    for table in Base.metadata.sorted_tables:
        if table.name in _OWN_TABLES or table.name == "patients":
            continue
        for fk in table.foreign_keys:
            if fk.column.table.name == "patients":
                col = fk.parent
                for i in range(0, len(patient_ids), 500):
                    chunk = patient_ids[i:i + 500]
                    held.update(r[0] for r in db.query(col).filter(col.in_(chunk)).distinct().all())
    return held


def _delete_recalls(db: Session, criterion) -> None:
    """Removes the worklist history (contacts, closures) of the recalls matching `criterion`; a bulk delete skips the cascade."""
    ids = [r[0] for r in db.query(PatientRecall.id).filter(criterion).all()]
    for i in range(0, len(ids), 500):
        db.query(RecallAction).filter(RecallAction.recall_id.in_(ids[i:i + 500])).delete(synchronize_session=False)


def undo_batch(db: Session, batch: DataImportBatch) -> tuple:
    """Removes the patients this batch created that have no other records, plus their recalls. Patients that existed
    before the import, or have since gained clinical/scheduling records, are never touched. Returns (removed, kept)."""
    created = [r[0] for r in db.query(DataImportBatchPatient.patient_id).filter(DataImportBatchPatient.batch_id == batch.id).all()]
    held = _patients_with_other_records(db, created) if created else set()
    removable = [pid for pid in created if pid not in held]
    for i in range(0, len(removable), 500):
        chunk = removable[i:i + 500]
        _delete_recalls(db, PatientRecall.patient_id.in_(chunk))
        db.query(PatientRecall).filter(PatientRecall.patient_id.in_(chunk)).delete(synchronize_session=False)
        from ehr.models.safety import PatientSafetyFlag
        db.query(PatientSafetyFlag).filter(PatientSafetyFlag.patient_id.in_(chunk)).delete(synchronize_session=False)
        db.query(DataImportBatchPatient).filter(DataImportBatchPatient.patient_id.in_(chunk)).delete(synchronize_session=False)
        db.query(Patient).filter(Patient.id.in_(chunk)).delete(synchronize_session=False)
    # Recalls this batch added to patients that already existed.
    _delete_recalls(db, PatientRecall.import_batch_id == batch.id)
    db.query(PatientRecall).filter(PatientRecall.import_batch_id == batch.id).delete(synchronize_session=False)
    batch.status, batch.patients_undone, batch.patients_kept_on_undo = "undone", len(removable), len(held)
    return len(removable), len(held)

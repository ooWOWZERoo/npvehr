"""Unit tests for the recall-report import (ehr.services.recall_import). All data here is fabricated (see
tests/fixtures/recall_sample.*): no real patient information belongs in this repository."""
import os
import tempfile
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from ehr.models.database import Base, Patient, Problem
from ehr.models.imports import DataImportBatch, DataImportBatchPatient, PatientRecall
from ehr.services import recall_import as ri

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory(prefix="npvehr-importtest-") as tmp:
        engine = create_engine(f"sqlite:///{os.path.join(tmp, 't.db')}")
        Base.metadata.create_all(bind=engine)
        with Session(engine) as s:
            yield s
        engine.dispose()


def _batch(db):
    b = DataImportBatch(status="staged"); db.add(b); db.flush()
    return b


def test_normalisers():
    assert ri.title_name("MCDONALD") == "McDonald" and ri.title_name("O'NEIL") == "O'Neil"
    assert ri.title_name("ann-marie smith".upper()) == "Ann-Marie Smith"
    assert ri.title_name("DeLuca") == "DeLuca"                                   # deliberate mixed case is kept
    assert ri.parse_date("3/7/1958") == date(1958, 3, 7) and ri.parse_date("1958-03-07") == date(1958, 3, 7)
    assert ri.parse_date("13/45/2020") is None and ri.parse_date("Never") is None
    assert ri.norm_phone("610-555-0102") == ("(610) 555-0102", True)
    assert ri.norm_phone("1 (610) 555-0102") == ("(610) 555-0102", True)
    assert ri.norm_phone("5551234") == ("5551234", False) and ri.norm_phone("") == (None, True)
    assert ri.norm_email("Pat.Sample@Example.com") == ("pat.sample@example.com", True)
    assert ri.norm_email("not-an-email") == (None, False)
    assert ri.parse_recall_type("12 Months Child") == ("12 Months Child", 12, "child")
    assert ri.parse_recall_type("3 month adult") == ("3 month adult", 3, "adult")
    assert ri.parse_recall_type("Appt Time") == ("Appt Time", None, None)
    assert ri.parse_recall_type(None)[0] == "Unspecified"


def test_csv_and_xls_read_identically_and_pick_up_section_recall_types():
    csv_rows = ri.read_rows("recall_sample.csv", (FIXTURES / "recall_sample.csv").read_bytes())
    xls_rows = ri.read_rows("recall_sample.xls", (FIXTURES / "recall_sample.xls").read_bytes())
    assert len(csv_rows) == len(xls_rows) == 6
    assert [(r["last"], r["first"], r["recall_type"]) for r in csv_rows] == [(r["last"], r["first"], r["recall_type"]) for r in xls_rows]
    assert [r["recall_type"] for r in csv_rows] == ["12 Month Adult", "12 Month Adult", "Appt Time", "6 Months Child", "6 Months Child", "6 Months Child"]
    assert csv_rows[0]["row"] == 3                                               # 1-based source row, for the preview's skipped list


def test_unreadable_files_are_refused_with_a_clear_message():
    with pytest.raises(ri.ImportFileError, match="xlsx"):
        ri.read_rows("x.xlsx", b"PK")
    with pytest.raises(ri.ImportFileError, match=".xls or .csv"):
        ri.read_rows("x.pdf", b"%PDF")
    with pytest.raises(ri.ImportFileError, match="header row"):
        ri.read_rows("x.csv", b"a,b,c\n1,2,3\n")
    with pytest.raises(ri.ImportFileError, match="Excel"):
        ri.read_rows("x.xls", b"not really an xls")


def test_plan_maps_fields_merges_repeats_and_skips_bad_rows(db):
    plan = ri.build_plan(db, ri.read_rows("recall_sample.csv", (FIXTURES / "recall_sample.csv").read_bytes()))
    assert plan.rows_total == 6
    assert len(plan.new) == 3 and len(plan.matched) == 0
    assert sorted(r for r, _ in plan.skipped) == [9, 10]                          # a blank last name, and an impossible date of birth
    assert {reason for _, reason in plan.skipped} == {"missing first or last name", "unreadable date of birth"}
    assert len(plan.recalls) == 4                                                 # Samplesmith has two recalls (12 Month Adult + Appt Time)
    assert plan.warnings["merged_rows"] == 1
    pat = next(d for d in plan.new.values() if d.fields["last_name"] == "Samplesmith").fields
    assert pat == {"first_name": "Pat", "last_name": "Samplesmith", "date_of_birth": "1958-03-07", "gender": "F",
                   "phone": "(610) 555-0101", "email": "pat.sample@example.com", "address": "1 Test Road, Apt 2",
                   "city": "Testville", "state": "PA", "zip_code": "19400"}
    kim = next(d for d in plan.new.values() if d.fields["last_name"] == "O'Neil").fields
    assert kim["gender"] is None and kim["email"] is None and kim["phone"] == "5551234" and kim["zip_code"] == "194"
    assert plan.warnings["odd_phone"] == 1 and plan.warnings["odd_zip"] == 1 and plan.warnings["invalid_email"] == 1
    never = [r for r in plan.recalls if r.never]
    assert len(never) == 2 and all(r.last_exam is None for r in never)
    types = {(r.recall_type, r.interval_months, r.audience) for r in plan.recalls}
    assert ("12 Month Adult", 12, "adult") in types and ("6 Months Child", 6, "child") in types and ("Appt Time", None, None) in types


def test_apply_creates_patients_and_recalls_leaves_opt_ins_off_and_is_idempotent(db):
    rows = ri.read_rows("recall_sample.csv", (FIXTURES / "recall_sample.csv").read_bytes())
    batch = _batch(db)
    ri.apply_plan(db, ri.build_plan(db, rows), batch); db.commit()
    assert (batch.patients_created, batch.recalls_created, batch.rows_skipped) == (3, 4, 2)
    assert db.scalar(select(func.count()).select_from(Patient)) == 3
    assert db.scalar(select(func.count()).select_from(Patient).where(Patient.sms_opt_in.is_(True) | Patient.email_opt_in.is_(True))) == 0
    sam = db.scalar(select(Patient).where(Patient.last_name == "McDonald"))
    assert (sam.first_name, sam.gender, sam.phone, sam.mrn) == ("Sam", "M", "(610) 555-0102", None)
    rec = db.scalar(select(PatientRecall).where(PatientRecall.patient_id == sam.id))
    assert (rec.due_date, rec.never_examined, rec.last_exam_date, rec.next_appt_date) == (date(2026, 3, 3), True, None, None)
    first = db.scalar(select(PatientRecall).join(Patient).where(Patient.last_name == "Samplesmith", PatientRecall.recall_type == "12 Month Adult"))
    assert (first.last_exam_date, first.next_appt_date) == (date(2023, 6, 15), date(2024, 3, 19))

    again = ri.build_plan(db, rows)                                               # same file a second time adds nothing
    assert len(again.new) == 0 and len(again.matched) == 3 and len(again.recalls) == 0 and again.recalls_already_on_file == 4


def test_existing_patients_are_matched_never_overwritten(db):
    db.add(Patient(first_name="Pat", last_name="SAMPLESMITH", date_of_birth="1958-03-07", phone="999-OLD", gender="F"))
    db.commit()
    plan = ri.build_plan(db, ri.read_rows("recall_sample.csv", (FIXTURES / "recall_sample.csv").read_bytes()))
    assert len(plan.matched) == 1 and len(plan.new) == 2                           # case-insensitive match on name + DOB
    ri.apply_plan(db, plan, _batch(db)); db.commit()
    pat = db.scalar(select(Patient).where(Patient.date_of_birth == "1958-03-07"))
    assert pat.phone == "999-OLD"                                                  # untouched
    assert db.scalar(select(func.count()).select_from(PatientRecall).where(PatientRecall.patient_id == pat.id)) == 2


def test_undo_removes_only_untouched_created_patients_and_their_recalls(db):
    keeper = Patient(first_name="Pre", last_name="Existing", date_of_birth="1960-01-01")
    db.add(keeper); db.commit()
    batch = _batch(db)
    ri.apply_plan(db, ri.build_plan(db, ri.read_rows("recall_sample.csv", (FIXTURES / "recall_sample.csv").read_bytes())), batch)
    batch.status = "imported"; db.commit()
    now_busy = db.scalar(select(Patient).where(Patient.last_name == "McDonald"))   # gained a real record after the import
    db.add(Problem(patient_id=now_busy.id, diagnosis_name="Test diagnosis")); db.commit()

    removed, kept = ri.undo_batch(db, batch); db.commit()
    assert (removed, kept) == (2, 1) and batch.status == "undone"
    names = {p.last_name for p in db.scalars(select(Patient)).all()}
    assert names == {"Existing", "McDonald"}                                       # the busy patient and the pre-existing one survive
    assert db.scalar(select(func.count()).select_from(DataImportBatchPatient).where(DataImportBatchPatient.patient_id != now_busy.id)) == 0
    assert db.scalar(select(func.count()).select_from(PatientRecall).where(PatientRecall.import_batch_id == batch.id)) == 0

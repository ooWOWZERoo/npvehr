"""Unit tests for the medication-list import (ehr.services.medication_import). Fabricated data only."""
import os
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ehr.models.database import Base, FieldChangeAuditEvent, Patient
from ehr.models.imports import DataImportBatch, DataImportBatchRecord
from ehr.models.medications import MedicationListReview, PatientMedication
from ehr.services import medication_import as svc
from ehr.services import medications as med

SAMPLE = (Path(__file__).parent / "fixtures" / "medication_import_sample.csv").read_bytes()


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory(prefix="npvehr-medimport-") as tmp:
        engine = create_engine(f"sqlite:///{os.path.join(tmp, 't.db')}")
        Base.metadata.create_all(bind=engine)
        with Session(engine) as s:
            s.add_all([Patient(first_name="Bob", last_name="Smith", date_of_birth="1972-07-22"),
                       Patient(first_name="Emma", last_name="Brown", date_of_birth="2005-01-12")])
            s.commit()
            yield s
        engine.dispose()


def _apply(db):
    plan = svc.build_plan(db, svc.read_rows("x.csv", SAMPLE))
    b = DataImportBatch(kind=svc.KIND, status="staged"); db.add(b); db.flush()
    svc.apply_plan(db, plan, b, 7)
    b.status = "imported"; db.commit()
    return plan, b


def test_plan_matches_patients_and_skips_with_reasons(db):
    plan = svc.build_plan(db, svc.read_rows("x.csv", SAMPLE))
    assert plan.rows_total == 9 and len(plan.drafts) == 5 and len(plan.patients_matched) == 2
    reasons = sorted(r for _, r in plan.skipped)
    assert reasons == ["Missing name or unreadable date of birth", "No matching patient in the system", "No medication name"]
    assert plan.repeats_in_file == 1
    assert plan.warnings["bad_eye"] == 1 and plan.warnings["bad_date"] == 1 and plan.warnings["bad_status"] == 1
    assert plan.unclassified == 4                                    # four active meds, no reviewed drug class exists
    lines = " ".join(plan.summary_lines)
    assert "Smith" not in lines and "Brown" not in lines            # no patient identifiers in the notes


def test_apply_maps_fields_marks_imported_and_is_not_a_review(db):
    _apply(db)
    bob = db.query(Patient).filter_by(last_name="Smith").one()
    meds = {m.name: m for m in db.query(PatientMedication).filter_by(patient_id=bob.id)}
    t = meds["Timolol 0.5%"]
    assert (t.strength, t.route, t.eye, t.frequency, t.indication, t.status, t.source) == ("0.5%", "ophthalmic", "OD", "twice daily", "glaucoma", "active", "imported")
    assert t.start_date.isoformat() == "2024-01-15" and t.recorded_by_user_id == 7
    assert meds["Warfarin 5 mg"].status == "stopped" and meds["Metformin"].status == "active"
    emma = db.query(PatientMedication).filter_by(name="Ibuprofen").one()
    assert emma.eye is None and emma.status == "active"             # bad eye dropped; unknown status -> active
    assert db.query(MedicationListReview).count() == 0              # an import is not a review
    assert med.review_status(db, bob.id, "medications")["state"] == "never"
    assert db.query(FieldChangeAuditEvent).filter_by(table_name="patient_medications").count() >= 5


def test_rerun_adds_nothing_and_existing_medications_are_untouched(db):
    bob = db.query(Patient).filter_by(last_name="Smith").one()
    pre = med.add_medication(db, bob.id, 1, {"name": "metformin", "strength": "500 MG"}); db.commit()
    plan = svc.build_plan(db, svc.read_rows("x.csv", SAMPLE))
    assert plan.already_on_file == 1 and len(plan.drafts) == 4
    _apply(db)
    again = svc.build_plan(db, svc.read_rows("x.csv", SAMPLE))
    assert not again.drafts and again.already_on_file == 6          # five imported plus the repeated Timolol row
    db.refresh(pre)
    assert pre.name == "metformin" and pre.source == "staff"


def test_ambiguous_patients_are_skipped(db):
    db.add(Patient(first_name="Bob", last_name="Smith", date_of_birth="1972-07-22")); db.commit()
    plan = svc.build_plan(db, svc.read_rows("x.csv", SAMPLE))
    assert any(r == "More than one matching patient" for _, r in plan.skipped)
    assert {d.patient_id for d in plan.drafts} == {db.query(Patient).filter_by(last_name="Brown").one().id}


def test_undo_removes_untouched_imports_and_keeps_edited_ones(db):
    _, b = _apply(db)
    m = db.query(PatientMedication).filter_by(name="Metformin").one()
    m.updated_at = m.recorded_at + timedelta(hours=1)                # edited afterwards
    db.commit()
    removed, kept = svc.undo_batch(db, b, 7); db.commit()
    assert (removed, kept) == (4, 1) and b.status == "undone"
    assert [x.name for x in db.query(PatientMedication)] == ["Metformin"]
    assert db.query(DataImportBatchRecord).count() == 0


def test_header_errors_and_template():
    with pytest.raises(svc.ImportFileError):
        svc.read_rows("x.csv", b"foo,bar\n1,2\n")
    with pytest.raises(svc.ImportFileError):
        svc.read_rows("x.csv", b"LastName,FirstName,DOB\nA,B,1990-01-01\n")      # no Medication column
    assert svc.read_rows("t.csv", svc.template_csv().encode())[0]["name"] == "Latanoprost 0.005%"

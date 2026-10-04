"""Unit tests for merging duplicate patients (ehr.services.patient_merge). Fabricated data; throwaway SQLite, no browser."""
import json
import os
import tempfile
from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from ehr.models.care_coordination import OutsidePractitioner, PatientPrimaryCare
from ehr.models.database import Appointment, Base, EyeExam, FieldChangeAuditEvent, Patient
from ehr.models.imports import DataImportBatch, DataImportBatchPatient, PatientRecall
from ehr.models.medications import PatientMedication
from ehr.models.patient_merge import PatientMergeEvent
from ehr.models.safety import PatientSafetyFlag, SafetyFlagType
from ehr.services import patient_merge as svc

KEEP, DUP = 1, 2


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory(prefix="npvehr-mergetest-") as tmp:
        engine = create_engine(f"sqlite:///{os.path.join(tmp, 't.db')}")
        Base.metadata.create_all(bind=engine)
        with Session(engine) as s:
            s.add_all([
                Patient(id=KEEP, first_name="Pat", last_name="Sample", date_of_birth="1970-01-31", phone="(610) 555-0100", mrn="K-1",
                        allergies="Sulfa", balance_due=10.0, sms_opt_in=True, email_opt_in=True, created_at=datetime(2020, 1, 1)),
                Patient(id=DUP, first_name="Patricia", last_name="Sample", date_of_birth="1970-01-31", phone=None, email="pat@example.com",
                        mrn="D-9", allergies="Penicillin", balance_due=5.5, sms_opt_in=True, email_opt_in=False, created_at=datetime(2021, 1, 1))])
            s.add_all([SafetyFlagType(id=1, code="a", label="A", is_active=True), SafetyFlagType(id=2, code="b", label="B", is_active=True),
                       OutsidePractitioner(id=1, first_name="Dr", last_name="One"), OutsidePractitioner(id=2, first_name="Dr", last_name="Two"),
                       DataImportBatch(id=1, kind="recall_report", status="imported")])
            s.commit()
            yield s
        engine.dispose()


def _seed_children(db):
    db.add_all([
        Appointment(patient_id=DUP, provider_id=1, scheduled_at=datetime(2026, 5, 1)), Appointment(patient_id=KEEP, provider_id=1, scheduled_at=datetime(2026, 4, 1)),
        EyeExam(patient_id=DUP, provider_id=1, exam_date="2026-01-01", signed_at=datetime(2026, 1, 2), signed_by_user_id=3),
        PatientMedication(patient_id=DUP, name="Latanoprost"),
        PatientSafetyFlag(patient_id=KEEP, flag_type_id=1, status="yes"), PatientSafetyFlag(patient_id=DUP, flag_type_id=1, status="no"),
        PatientSafetyFlag(patient_id=DUP, flag_type_id=2, status="yes"),
        PatientPrimaryCare(patient_id=KEEP, practitioner_id=1), PatientPrimaryCare(patient_id=DUP, practitioner_id=2),
        PatientRecall(patient_id=KEEP, recall_type="12 Month Adult", due_date=date(2026, 3, 1)),
        PatientRecall(patient_id=DUP, recall_type="12 Month Adult", due_date=date(2026, 3, 1)),     # same key: collides
        PatientRecall(patient_id=DUP, recall_type="6 Months Adult", due_date=date(2026, 9, 1)),     # different: moves
        DataImportBatchPatient(batch_id=1, patient_id=DUP)])
    db.commit()


def _count(db, model, **kw):
    return db.query(model).filter_by(**kw).count()


def test_linked_tables_are_found_from_the_metadata():
    names = {t.name for t, _, _ in svc.linked_tables()}
    assert {"appointments", "eye_exams", "prescriptions", "patient_medications", "patient_safety_flags", "patient_recalls", "patient_primary_care"} <= names
    assert "patients" not in names


def test_merge_moves_records_sets_aside_collisions_and_removes_the_duplicate(db):
    _seed_children(db)
    ev = svc.merge(db, KEEP, DUP, 7, "Same person registered twice"); db.commit()
    assert db.get(Patient, DUP) is None and db.get(Patient, KEEP) is not None
    assert _count(db, Appointment, patient_id=KEEP) == 2 and _count(db, EyeExam, patient_id=KEEP) == 1 and _count(db, PatientMedication, patient_id=KEEP) == 1
    exam = db.query(EyeExam).one()
    assert exam.signed_at is not None and exam.signed_by_user_id == 3                      # a signed exam moves with its signature intact
    flags = {f.flag_type_id: f.status for f in db.query(PatientSafetyFlag).filter_by(patient_id=KEEP)}
    assert flags == {1: "yes", 2: "yes"}                                                  # the kept chart's answer wins; the other flag moves
    assert [(p.patient_id, p.practitioner_id) for p in db.query(PatientPrimaryCare)] == [(KEEP, 1)]
    assert sorted((r.recall_type, r.patient_id) for r in db.query(PatientRecall)) == [("12 Month Adult", KEEP), ("6 Months Adult", KEEP)]
    assert _count(db, DataImportBatchPatient) == 0                                         # import-batch membership never transfers
    d = json.loads(ev.details_json)
    assert set(d["dropped"]) == {"patient_safety_flags", "patient_primary_care", "patient_recalls", "data_import_batch_patients"}
    assert ev.moved_total == 5 and ev.dropped_total == 4 and ev.status == "merged" and ev.merged_label == "Sample, Patricia"
    assert db.query(FieldChangeAuditEvent).filter_by(table_name="patient_merge_events", record_id=ev.id).count() >= 1


def test_demographics_blanks_filled_text_combined_balance_added_optins_anded_mrn_kept(db):
    ev = svc.merge(db, KEEP, DUP, 7, "Duplicate chart"); db.commit()
    keep = db.get(Patient, KEEP)
    assert keep.first_name == "Pat" and keep.mrn == "K-1"                                  # differing scalars keep the kept chart's by default
    assert keep.email == "pat@example.com"                                                # a blank on the kept chart is filled from the duplicate
    assert keep.allergies == "Sulfa" + svc.JOINER + "Penicillin"                          # conflicting free text is combined, not lost
    assert keep.balance_due == 15.5
    assert keep.sms_opt_in is True and keep.email_opt_in is False                         # opted in only if BOTH were
    assert keep.phone == "(610) 555-0100"


def test_explicit_field_choices_and_mrn_transfer(db):
    svc.merge(db, KEEP, DUP, 7, "Duplicate chart", {"first_name": "dup", "mrn": "dup", "allergies": "keep"}); db.commit()
    keep = db.get(Patient, KEEP)
    assert keep.first_name == "Patricia" and keep.mrn == "D-9" and keep.allergies == "Sulfa"


def test_undo_restores_the_duplicate_fields_rows_and_set_aside_rows(db):
    _seed_children(db)
    ev = svc.merge(db, KEEP, DUP, 7, "Duplicate chart", {"mrn": "dup"}); db.commit()
    out = svc.undo(db, ev, 8); db.commit()
    assert out["skipped"] == 0 and ev.status == "undone" and ev.undone_by_user_id == 8
    dup, keep = db.get(Patient, DUP), db.get(Patient, KEEP)
    assert dup is not None and (dup.first_name, dup.mrn, dup.email, dup.balance_due, dup.created_at) == ("Patricia", "D-9", "pat@example.com", 5.5, datetime(2021, 1, 1))
    assert (keep.mrn, keep.allergies, keep.balance_due, keep.email, keep.email_opt_in) == ("K-1", "Sulfa", 10.0, None, True)
    assert _count(db, Appointment, patient_id=DUP) == 1 and _count(db, Appointment, patient_id=KEEP) == 1
    assert _count(db, EyeExam, patient_id=DUP) == 1 and _count(db, PatientMedication, patient_id=DUP) == 1
    assert {f.flag_type_id: f.status for f in db.query(PatientSafetyFlag).filter_by(patient_id=DUP)} == {1: "no", 2: "yes"}
    assert db.query(PatientPrimaryCare).filter_by(patient_id=DUP).one().practitioner_id == 2 and _count(db, PatientRecall, patient_id=DUP) == 2
    assert _count(db, DataImportBatchPatient, patient_id=DUP) == 1
    with pytest.raises(svc.MergeError):
        svc.undo(db, ev, 8)                                                                # already undone


def test_undo_skips_rows_that_changed_since_and_counts_them(db):
    _seed_children(db)
    ev = svc.merge(db, KEEP, DUP, 7, "Duplicate chart"); db.commit()
    db.delete(db.query(PatientMedication).one()); db.commit()                              # deleted after the merge
    out = svc.undo(db, ev, 8); db.commit()
    assert out["skipped"] == 1 and _count(db, PatientMedication) == 0


def test_guards_and_nothing_changes_on_refusal(db):
    with pytest.raises(svc.MergeError):
        svc.merge(db, KEEP, KEEP, 7, "same chart twice")
    with pytest.raises(svc.MergeError):
        svc.merge(db, KEEP, 99, 7, "no such patient")
    with pytest.raises(svc.MergeError):
        svc.merge(db, KEEP, DUP, 7, "no")
    assert db.get(Patient, DUP) is not None and db.query(PatientMergeEvent).count() == 0


def test_exact_name_and_birth_date_is_the_strongest_duplicate_signal(db):
    db.add(Patient(id=5, first_name="PAT", last_name="sample", date_of_birth="1970-01-31")); db.commit()
    groups = svc.duplicate_candidates(db)
    assert any(g["reason"] == "Same name and date of birth" and sorted(p.id for p in g["patients"]) == [1, 5] for g in groups)


def test_duplicate_candidates_and_preview(db):
    db.add_all([Patient(id=3, first_name="Lee", last_name="Other", date_of_birth="1980-02-02", phone="610-555-0188"),
                Patient(id=4, first_name="Leigh", last_name="Another", date_of_birth="1980-02-02", phone="(610) 555-0188")]); db.commit()
    _seed_children(db)
    groups = svc.duplicate_candidates(db)
    reasons = {g["reason"]: sorted(p.id for p in g["patients"]) for g in groups}
    assert reasons == {"Similar first name, same last name and date of birth": [1, 2], "Same phone and date of birth": [3, 4]}
    pv = {r["table"]: r for r in svc.preview(db, KEEP, DUP)}
    assert pv["appointments"]["keep"] == 1 and pv["appointments"]["dup"] == 1 and pv["appointments"]["aside"] == 0
    assert pv["patient_safety_flags"]["aside"] == 1 and pv["patient_primary_care"]["aside"] == 1 and pv["data_import_batch_patients"]["aside"] == 1

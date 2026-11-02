"""Unit tests for the exam's structured Prescription & treatment step (ehr/services/exam_prescribing.py): no browser, a throwaway
SQLite file per test. The browser flow is covered end to end in test_smoke.py."""
import json
import os
import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ehr.models.database import Base, DiagnosticOrder, DiagnosticTest, EyeExam, Patient
from ehr.models.drug_catalog import DrugCatalogEntry, DrugDosage, DrugFrequency
from ehr.models.medications import PatientMedication
from ehr.services import exam_prescribing as rx


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory(prefix="npvehr-rxstep-") as tmp:
        engine = create_engine(f"sqlite:///{os.path.join(tmp, 't.db')}")
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine)() as session:
            yield session
        engine.dispose()


@pytest.fixture
def world(db):
    """A patient, an exam, an eye-drop and an oral drug with one dosage each, two frequencies, and three diagnostic tests."""
    patient = Patient(first_name="Test", last_name="Patient", date_of_birth="1980-01-01")
    db.add(patient); db.flush()
    exam = EyeExam(patient_id=patient.id, provider_id=1, exam_date="2026-11-03")
    db.add(exam)
    drop = DrugCatalogEntry(name="Zzdrop", name_lower="zzdrop", default_route="ophthalmic", source="manual", is_active=True)
    pill = DrugCatalogEntry(name="Zzpill", name_lower="zzpill", default_route="oral", source="manual", is_active=True)
    db.add_all([drop, pill]); db.flush()
    drop_dose = DrugDosage(drug_id=drop.id, label="0.1% ophthalmic solution")
    pill_dose = DrugDosage(drug_id=pill.id, label="10 mg oral tablet")
    qid = DrugFrequency(route_group="any", label="4 times a day", sort_order=1)
    bid = DrugFrequency(route_group="any", label="twice a day", sort_order=2)
    db.add_all([drop_dose, pill_dose, qid, bid])
    tests = [DiagnosticTest(code=c, display_name=c.upper(), calendar_abbreviation=c.upper(), active=a, cpt_code=cpt, display_order=i)
             for i, (c, a, cpt) in enumerate((("oct", True, "92134"), ("vf", True, "92083"), ("old", False, None)))]
    db.add_all(tests); db.flush()
    return {"patient": patient, "exam": exam, "drop": drop, "pill": pill, "drop_dose": drop_dose, "pill_dose": pill_dose, "qid": qid, "bid": bid}


def _catalog_entry(w, **kw):
    e = {"drug_id": w["drop"].id, "dosage_id": w["drop_dose"].id, "frequency_id": w["qid"].id, "eye": "OU"}
    e.update(kw)
    return e


def test_nothing_staged_means_nothing_prescribed(db):
    assert rx.parse_prescribed(db, None) == [] and rx.parse_prescribed(db, "") == [] and rx.parse_prescribed(db, "  []  ") == []


def test_a_catalog_choice_and_a_custom_entry_are_validated_by_the_intake_rules(db, world):
    items = rx.parse_prescribed(db, json.dumps([
        _catalog_entry(world, indication="dry eye", note="x 7 days"),
        {"is_custom": True, "name": "Homemade drops", "dosage_text": "1 drop", "frequency_text": "nightly", "eye": "NA"}]))
    first, second = items
    assert (first["values"]["name"], first["values"]["strength"], first["values"]["frequency"], first["values"]["eye"]) == \
           ("Zzdrop", "0.1% ophthalmic solution", "4 times a day", "OU")
    assert first["indication"] == "dry eye" and first["note"] == "x 7 days" and first["values"]["is_custom"] is False
    assert second["values"]["is_custom"] is True and second["values"]["eye"] == "NA" and second["values"]["drug_id"] is None


def test_an_oral_drug_is_always_recorded_by_mouth(db, world):
    item = rx.parse_prescribed(db, json.dumps([_catalog_entry(world, drug_id=world["pill"].id, dosage_id=world["pill_dose"].id, eye="OU")]))[0]
    assert item["values"]["eye"] == "PO"


@pytest.mark.parametrize("raw", ["not json", "{}", "[1, 2]", '"text"'])
def test_unreadable_staged_data_is_refused_with_a_sentence_for_the_user(db, raw):
    with pytest.raises(ValueError, match="could not be read"):
        rx.parse_prescribed(db, raw)


def test_a_bad_entry_is_refused_and_named(db, world):
    with pytest.raises(ValueError, match="Prescribed medication Homemade: Eye must be"):
        rx.parse_prescribed(db, json.dumps([{"is_custom": True, "name": "Homemade", "eye": "ZZ"}]))
    with pytest.raises(ValueError, match="Prescribed medication #1: Choose a dosage that belongs to the selected medication"):
        rx.parse_prescribed(db, json.dumps([_catalog_entry(world, dosage_id=world["pill_dose"].id)]))      # a dosage of another drug
    with pytest.raises(ValueError, match="malformed catalog choice"):
        rx.parse_prescribed(db, json.dumps([_catalog_entry(world, drug_id="abc")]))
    with pytest.raises(ValueError, match="Eye must be"):
        rx.parse_prescribed(db, json.dumps([_catalog_entry(world, eye="XX")]))


def test_there_is_a_cap_on_medications_per_visit(db, world):
    with pytest.raises(ValueError, match="At most 20"):
        rx.parse_prescribed(db, json.dumps([_catalog_entry(world)] * 21))


def test_prescribed_medications_join_the_list_linked_to_the_exam_and_started_on_the_exam_date(db, world):
    items = rx.parse_prescribed(db, json.dumps([_catalog_entry(world, indication="blepharitis")]))
    created = rx.add_prescribed(db, world["exam"], user_id=1, items=items)
    db.commit()
    m = db.query(PatientMedication).one()
    assert created == [m]
    assert (m.patient_id, m.name, m.strength, m.frequency, m.eye, m.status) == (world["patient"].id, "Zzdrop", "0.1% ophthalmic solution", "4 times a day", "OU", "active")
    assert (m.source, m.prescribed_exam_id, m.indication) == ("prescribed", world["exam"].id, "blepharitis")
    assert str(m.start_date) == "2026-11-03" and (m.drug_id, m.dosage_id, m.frequency_id) == (world["drop"].id, world["drop_dose"].id, world["qid"].id)


def test_prescribing_what_is_already_active_replaces_the_old_entry_but_a_different_strength_does_not(db, world):
    other = DrugDosage(drug_id=world["drop"].id, label="0.2% ophthalmic solution")
    db.add(other); db.flush()
    old = PatientMedication(patient_id=world["patient"].id, name="ZZDROP", strength="0.1% ophthalmic solution", eye="OU", status="active", source="staff")
    other_strength = PatientMedication(patient_id=world["patient"].id, name="Zzdrop", strength="0.5% ophthalmic solution", eye="OU", status="active", source="staff")
    other_eye = PatientMedication(patient_id=world["patient"].id, name="Zzdrop", strength="0.1% ophthalmic solution", eye="OD", status="active", source="staff")
    db.add_all([old, other_strength, other_eye]); db.flush()
    rx.add_prescribed(db, world["exam"], 1, rx.parse_prescribed(db, json.dumps([_catalog_entry(world, frequency_id=world["bid"].id)])))
    db.commit()
    db.refresh(old); db.refresh(other_strength); db.refresh(other_eye)
    assert (old.status, other_strength.status, other_eye.status) == ("stopped", "active", "active")
    active = db.query(PatientMedication).filter(PatientMedication.status == "active").all()
    assert sorted((m.eye, m.frequency or "") for m in active) == [("OD", ""), ("OU", ""), ("OU", "twice a day")]


def test_tests_to_order_keeps_known_active_codes_once_and_skips_what_this_exam_already_ordered(db, world):
    vf = db.query(DiagnosticTest).filter(DiagnosticTest.code == "vf").one()
    db.add(DiagnosticOrder(patient_id=world["patient"].id, diagnostic_test_id=vf.id, ordered_exam_id=world["exam"].id)); db.flush()
    picked = rx.tests_to_order(db, world["exam"], ["oct", "vf", "oct", "old", "nope", ""])
    assert [t.code for t in picked] == ["oct"]                      # vf already ordered, old is inactive, nope is unknown, oct once
    rows = rx.place_orders(db, world["exam"], user_id=7, tests=picked, notes="  before the next visit ")
    db.commit()
    order = db.query(DiagnosticOrder).filter(DiagnosticOrder.diagnostic_test_id == picked[0].id).one()
    assert rows == [order] and (order.status, order.ordered_exam_id, order.ordered_by_user_id, order.notes, order.follow_up_id) == \
           ("ordered", world["exam"].id, 7, "before the next visit", None)
    assert rx.tests_to_order(db, world["exam"], []) == []

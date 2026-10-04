"""Unit tests for the medication/allergy lists and the drug-class -> safety-flag derivation
(ehr.services.medications + the safety.evaluate hookup). Fabricated data; throwaway SQLite file, no browser."""
import os
import tempfile
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ehr.models.database import Base, FieldChangeAuditEvent
from ehr.models.medications import (MedicationClass, MedicationClassTerm, MedicationListReview, PatientMedication,
                                    SafetyFlagClassLink)
from ehr.models.safety import PatientSafetyFlag, SafetyFlagType, SafetyRule
from ehr.services import medications as med
from ehr.services import safety


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory(prefix="npvehr-medtest-") as tmp:
        engine = create_engine(f"sqlite:///{os.path.join(tmp, 't.db')}")
        Base.metadata.create_all(bind=engine)
        with Session(engine) as s:
            s.add(SafetyFlagType(code="blood_thinner", label="Takes a blood thinner", is_active=True))
            s.commit()
            yield s
        engine.dispose()


def _flag(db):
    return db.scalar(select(SafetyFlagType).where(SafetyFlagType.code == "blood_thinner"))


def _setup_class(db, reviewed=True, active=True, linked=True, terms=("warfarin", "coumadin")):
    c = MedicationClass(code="anticoag", label="Anticoagulant", is_active=active)
    db.add(c); db.flush()
    for t in terms:
        db.add(MedicationClassTerm(class_id=c.id, term=t, reviewed=reviewed, source_citation="Sample formulary" if reviewed else None))
    if linked:
        db.add(SafetyFlagClassLink(flag_type_id=_flag(db).id, class_id=c.id))
    db.add(SafetyRule(flag_type_id=_flag(db).id, warning_text="Bleeding risk", reviewed=True, is_active=True))
    db.commit()
    return c


def _add(db, pid, name, **kw):
    m = med.add_medication(db, pid, 1, {"name": name, **kw})
    db.commit()
    return m


def test_term_matching_is_whole_word_and_case_insensitive():
    assert med.term_matches("warfarin", "Warfarin 5 mg") and med.term_matches("coumadin", "COUMADIN")
    assert med.term_matches("eye drops", "lubricant Eye Drops")
    assert not med.term_matches("asa", "Casanova") and not med.term_matches("", "x")


def test_nothing_is_derived_until_terms_are_reviewed_class_active_and_linked(db):
    for kw in ({"reviewed": False}, {"active": False}, {"linked": False}):
        _setup_class(db, **kw)
        _add(db, 1, "Warfarin 5 mg")
        assert med.derived_flags(db, 1) == {}
        assert safety.evaluate(db, 1, "") == []
        for t in (MedicationClassTerm, SafetyFlagClassLink, MedicationClass, PatientMedication, SafetyRule):
            db.query(t).delete()
        db.commit()


def test_reviewed_linked_term_derives_the_flag_and_the_safety_rule_fires(db):
    _setup_class(db)
    m = _add(db, 1, "Coumadin 2mg")
    assert med.derived_flags(db, 1) == {_flag(db).id: ["Coumadin 2mg"]}
    assert [w["warning"] for w in safety.evaluate(db, 1, "")] == ["Bleeding risk"]
    assert safety.evaluate(db, 2, "") == []                                   # another patient is unaffected
    med.set_medication_status(db, m, "stopped", 1); db.commit()
    assert med.derived_flags(db, 1) == {} and safety.evaluate(db, 1, "") == []    # stopped meds don't count
    assert m.stop_date is not None
    med.set_medication_status(db, m, "active", 1); db.commit()
    assert m.stop_date is None and safety.evaluate(db, 1, "")


def test_medication_list_wins_over_a_manual_no(db):
    _setup_class(db)
    safety.set_flag(db, 1, _flag(db).id, "no", user_id=1); db.commit()
    assert safety.evaluate(db, 1, "") == []
    _add(db, 1, "Warfarin")
    assert [w["warning"] for w in safety.evaluate(db, 1, "")] == ["Bleeding risk"]
    safety.set_flag(db, 5, _flag(db).id, "yes", user_id=1); db.commit()      # a manual yes still works with no meds
    assert safety.evaluate(db, 5, "")


def test_unclassified_medications_are_reported_not_ignored(db):
    _setup_class(db)
    a, b = _add(db, 1, "Warfarin"), _add(db, 1, "Latanoprost")
    cls = med.classify(db, [a, b])
    assert cls[a.id] == ["Anticoagulant"] and cls[b.id] == []


def test_empty_list_is_unknown_until_reviewed_and_none_reported_needs_an_empty_list(db):
    assert med.review_status(db, 1, "medications")["state"] == "never"
    assert med.record_review(db, 1, "medications", "none_reported", 1); db.commit()
    assert med.review_status(db, 1, "medications")["state"] == "current"
    _add(db, 1, "Metformin")
    assert med.review_status(db, 1, "medications")["state"] == "stale"       # 'none reported' no longer matches the list
    assert med.record_review(db, 1, "medications", "none_reported", 1) is None
    assert med.record_review(db, 1, "medications", "no_changes", 1); db.commit()
    assert med.review_status(db, 1, "medications")["state"] == "current"
    assert med.record_review(db, 1, "bogus", "no_changes", 1) is None and med.record_review(db, 1, "allergies", "bogus", 1) is None
    r = db.query(MedicationListReview).order_by(MedicationListReview.id.desc()).first()
    assert r.item_count == 1
    assert med.review_status(db, 1, "medications", now=datetime.utcnow() + timedelta(days=400))["state"] == "stale"
    assert med.review_status(db, 1, "allergies")["state"] == "never"          # lists are reviewed independently


def test_add_edit_stop_allergy_validation_and_audit(db):
    assert med.add_medication(db, 1, 1, {"name": "  "}) is None and med.add_allergy(db, 1, 1, {"allergen": ""}) is None
    m = _add(db, 1, "Timolol", eye="od", strength="0.5%", start_date="2024-02-01")
    assert m.eye == "OD" and m.start_date.isoformat() == "2024-02-01" and m.source == "staff"
    assert med.update_medication(db, m, 1, {"name": "Timolol maleate", "eye": "bogus", "start_date": "not a date"})
    assert m.eye is None and m.start_date is None and m.name == "Timolol maleate"
    assert not med.update_medication(db, m, 1, {"name": ""})
    a = med.add_allergy(db, 1, 1, {"allergen": "Sulfa", "severity": "SEVERE", "reaction": "hives"}); db.commit()
    assert a.severity == "severe"
    med.set_allergy_status(db, a, "inactive", 1); db.commit()
    assert med.count_active(db, 1, "allergies") == 0
    tables = {e.table_name for e in db.query(FieldChangeAuditEvent)}
    assert {"patient_medications", "patient_allergies"} <= tables

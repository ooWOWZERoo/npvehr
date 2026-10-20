"""Unit tests for the medication/allergy lists and the drug-class -> safety-flag derivation
(ehr.services.medications + the safety.evaluate hookup). Fabricated data; throwaway SQLite file, no browser."""
import os
import tempfile
from types import SimpleNamespace
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ehr.models.database import Base, FieldChangeAuditEvent
from ehr.models.drug_catalog import DrugAlias, DrugCatalogEntry
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


def test_exam_form_reviews_are_recorded_against_the_exam_and_inconsistent_ones_are_ignored(db):
    exam = SimpleNamespace(id=42, patient_id=1)
    assert med.record_exam_reviews(db, exam, {"med_review_medications": "none_reported", "med_review_allergies": ""}, 5) == 1
    db.commit()
    r = db.query(MedicationListReview).one()
    assert (r.kind, r.outcome, r.exam_id, r.reviewed_by_user_id) == ("medications", "none_reported", 42, 5)
    assert [(x.kind, x.outcome) for x, _ in med.reviews_for_exam(db, 42)] == [("medications", "none_reported")]
    _add(db, 1, "Metformin")
    # 'none reported' is refused while the list has items; a bogus outcome is ignored; a valid one still records.
    n = med.record_exam_reviews(db, exam, {"med_review_medications": "none_reported", "med_review_allergies": "bogus"}, 5)
    assert n == 0
    assert med.record_exam_reviews(db, exam, {"med_review_medications": "updated", "med_review_allergies": "none_reported"}, 5) == 2
    assert med.record_exam_reviews(db, exam, {}, 5) == 0


def test_exam_card_summary_lists_active_items_and_review_state(db):
    _add(db, 1, "Latanoprost 0.005%", eye="ou", frequency="nightly")
    stopped = _add(db, 1, "Old drop"); med.set_medication_status(db, stopped, "stopped", 1)
    med.add_allergy(db, 1, 1, {"allergen": "Sulfa", "severity": "severe"}); db.commit()
    s = med.exam_card_summary(db, 1)
    assert [m["name"] for m in s["medications"]] == ["Latanoprost 0.005%"] and s["medications"][0]["eye"] == "OU"
    assert s["allergies"] == [{"allergen": "Sulfa", "reaction": None, "severity": "severe"}]
    assert s["medications_state"] == "never" and s["medications_reviewed_at"] is None
    med.record_review(db, 1, "medications", "no_changes", 1); db.commit()
    s = med.exam_card_summary(db, 1)
    assert s["medications_state"] == "current" and s["allergies_state"] == "never"
    assert med.exam_card_summary(db, 2) == {"medications": [], "allergies": [], "medications_state": "never", "medications_reviewed_at": None,
                                            "allergies_state": "never", "allergies_reviewed_at": None}


# ----------------------------------------------------------------------------- generic / brand equivalence in matching
def _catalog(db):
    """warfarin (generic) with brands Coumadin and Jantoven; the two brands as their own rows (as RxNorm's list carries them)."""
    g = DrugCatalogEntry(name="warfarin", name_lower="warfarin", source="rxnorm", tty="IN", is_active=True)
    c = DrugCatalogEntry(name="Coumadin", name_lower="coumadin", source="rxnorm", tty="BN", is_active=True)
    j = DrugCatalogEntry(name="Jantoven", name_lower="jantoven", source="rxnorm", tty="BN", is_active=True)
    t = DrugCatalogEntry(name="timolol", name_lower="timolol", source="rxnorm", tty="IN", is_active=True)
    db.add_all([g, c, j, t]); db.flush()
    db.add_all([DrugAlias(drug_id=g.id, alias="Coumadin", alias_lower="coumadin"), DrugAlias(drug_id=g.id, alias="Jantoven", alias_lower="jantoven"),
                DrugAlias(drug_id=t.id, alias="Betimol", alias_lower="betimol")])
    db.commit()
    return {"warfarin": g, "coumadin": c, "jantoven": j, "timolol": t}


def _linked(db, pid, name, drug, **kw):
    m = med.add_medication(db, pid, 1, {"name": name, **kw})
    m.drug_id = drug.id
    db.commit()
    return m


def test_a_term_listing_only_the_brand_matches_a_patient_recorded_under_the_generic(db):
    d = _catalog(db)
    _setup_class(db, terms=("coumadin",))
    m = _linked(db, 1, "warfarin", d["warfarin"])
    assert med.derived_flags(db, 1) == {_flag(db).id: ["warfarin"]}                      # the display name is still what was recorded
    assert med.classify(db, [m])[m.id] == ["Anticoagulant"]
    assert [w["warning"] for w in safety.evaluate(db, 1, "")] == ["Bleeding risk"]


def test_a_term_listing_only_the_generic_matches_a_patient_recorded_under_a_brand_and_brands_match_each_other(db):
    d = _catalog(db)
    _setup_class(db, terms=("warfarin",))
    m = _linked(db, 1, "Coumadin", d["coumadin"])
    assert med.classify(db, [m])[m.id] == ["Anticoagulant"]
    db.query(MedicationClassTerm).delete(); db.commit()
    # same class, new term: only the other brand is listed
    c = db.query(MedicationClass).one()
    db.add(MedicationClassTerm(class_id=c.id, term="jantoven", reviewed=True, source_citation="Sample formulary")); db.commit()
    assert med.classify(db, [m])[m.id] == ["Anticoagulant"]                              # recorded as Coumadin, term lists Jantoven


def test_an_older_free_text_row_is_expanded_only_when_its_whole_name_is_a_catalog_drug_or_brand(db):
    _catalog(db)
    _setup_class(db, terms=("coumadin",))
    exact = _add(db, 1, "Warfarin")
    with_strength = _add(db, 1, "Warfarin 5 mg")
    assert med.classify(db, [exact])[exact.id] == ["Anticoagulant"]
    assert med.classify(db, [with_strength])[with_strength.id] == []                     # free text with a strength is not guessed at


def test_equivalence_never_matches_unreviewed_terms_or_unrelated_drugs_and_never_raises(db):
    d = _catalog(db)
    _setup_class(db, terms=("coumadin",), reviewed=False)
    m = _linked(db, 1, "warfarin", d["warfarin"])
    assert med.classify(db, [m])[m.id] == [] and med.derived_flags(db, 1) == {}          # unreviewed: still silent
    db.query(MedicationClassTerm).delete(); db.query(SafetyFlagClassLink).delete(); db.query(MedicationClass).delete(); db.query(SafetyRule).delete(); db.commit()
    _setup_class(db, terms=("coumadin",))
    other = _linked(db, 1, "timolol", d["timolol"])                                      # its brands (Betimol) don't include Coumadin
    assert med.classify(db, [other])[other.id] == []
    ghost = _add(db, 1, "Ghost drug"); ghost.drug_id = 99999; db.commit()                # a dangling catalog link
    assert med.equivalent_names(db, [ghost]) == {ghost.id: ["Ghost drug"]} and med.classify(db, [ghost])[ghost.id] == []


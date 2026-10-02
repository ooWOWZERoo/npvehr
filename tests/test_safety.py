"""Unit tests for ehr.services.safety (ROS plan stage 5): the single decision of whether a clinical safety
warning fires, plus flag recording and acknowledgement bookkeeping. Throwaway SQLite file, no browser."""
import os
import tempfile
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from ehr.models.database import Base, FieldChangeAuditEvent
from ehr.models.safety import ExamSafetyAcknowledgement, PatientSafetyFlag, SafetyFlagType, SafetyRule
from ehr.services import safety


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory(prefix="npvehr-safetytest-") as tmp:
        engine = create_engine(f"sqlite:///{os.path.join(tmp, 't.db')}")
        Base.metadata.create_all(bind=engine)
        with Session(engine) as s:
            s.add_all([SafetyFlagType(code="pregnant", label="Pregnant", is_active=True),
                       SafetyFlagType(code="blood_thinner", label="Takes a blood thinner", is_active=True)])
            s.commit()
            yield s
        engine.dispose()


def _ft(db, code):
    return db.scalar(select(SafetyFlagType).where(SafetyFlagType.code == code))


def _rule(db, code, text, keyword=None, reviewed=True, active=True):
    r = SafetyRule(flag_type_id=_ft(db, code).id, warning_text=text, keyword=keyword, reviewed=reviewed, is_active=active)
    db.add(r); db.commit()
    return r


def test_a_rule_fires_only_when_flag_is_yes_and_rule_is_active_and_reviewed(db):
    _rule(db, "pregnant", "reviewed+active")
    _rule(db, "pregnant", "unreviewed", reviewed=False)
    _rule(db, "pregnant", "inactive", active=False)
    assert safety.evaluate(db, 7, "anything") == []                      # no flag recorded = unknown = silent
    safety.set_flag(db, 7, _ft(db, "pregnant").id, "no", user_id=1); db.commit()
    assert safety.evaluate(db, 7, "anything") == []                      # 'no' is silent too
    safety.set_flag(db, 7, _ft(db, "pregnant").id, "yes", user_id=1); db.commit()
    assert [w["warning"] for w in safety.evaluate(db, 7, "anything")] == ["reviewed+active"]
    assert safety.evaluate(db, 8, "anything") == []                      # another patient is unaffected


def test_keyword_rules_match_case_insensitively_and_the_feed_returns_them_unfiltered(db):
    _rule(db, "blood_thinner", "bleeding risk", keyword="Phenylephrine")
    safety.set_flag(db, 3, _ft(db, "blood_thinner").id, "yes", user_id=1); db.commit()
    assert safety.evaluate(db, 3, "Plan: dilate with PHENYLEPHRINE 2.5%")[0]["warning"] == "bleeding risk"
    assert safety.evaluate(db, 3, "Plan: refraction only") == []
    assert safety.evaluate(db, 3, "") == []
    assert safety.evaluate(db, 3, None)[0]["keyword"] == "Phenylephrine"  # feed for the client-side matcher


def test_flag_changes_are_audited_and_clearing_returns_to_unknown(db):
    pid = _ft(db, "pregnant").id
    assert safety.set_flag(db, 5, pid, "yes", user_id=9) is True
    assert safety.set_flag(db, 5, pid, "yes", user_id=9) is False         # no change, no audit noise
    assert safety.set_flag(db, 5, pid, "", user_id=9) is True             # clear => unknown
    db.commit()
    assert db.scalar(select(func.count()).select_from(PatientSafetyFlag).where(PatientSafetyFlag.patient_id == 5)) == 0
    events = db.scalars(select(FieldChangeAuditEvent).where(FieldChangeAuditEvent.table_name == "patient_safety_flags")).all()
    assert [(e.old_value, e.new_value) for e in events] == [(None, "yes"), ("yes", None)]
    safety_inactive = _ft(db, "pregnant"); safety_inactive.is_active = False; db.commit()
    assert safety.set_flag(db, 5, pid, "yes", user_id=9) is False         # inactive flag types can't be set


def test_acknowledgements_cover_only_currently_firing_rules_and_keep_a_text_snapshot(db):
    r1 = _rule(db, "pregnant", "warning one")
    r2 = _rule(db, "pregnant", "warning two")
    safety.set_flag(db, 4, _ft(db, "pregnant").id, "yes", user_id=1); db.commit()
    exam = SimpleNamespace(id=11, patient_id=4, chief_complaint="", assessment="", plan="", ros_notes="")
    assert {w["id"] for w in safety.unacknowledged(db, exam, [])} == {r1.id, r2.id}
    assert [w["id"] for w in safety.unacknowledged(db, exam, [str(r1.id), "junk"])] == [r2.id]
    assert safety.record_acknowledgements(db, exam, [r1.id, r2.id, 999999], user_id=2) == 2   # unknown id ignored
    assert safety.record_acknowledgements(db, exam, [r1.id], user_id=2) == 0                   # no double acknowledgement
    db.commit()
    assert safety.unacknowledged(db, exam, []) == []
    r1.warning_text = "edited later"; db.commit()
    snaps = {a.warning_text_snapshot for a, _ in safety.acknowledgements_for_exam(db, 11)}
    assert snaps == {"warning one", "warning two"}                                             # what was warned is preserved

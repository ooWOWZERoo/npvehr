"""Unit tests for the diabetic-retinopathy PCP letter rules (ehr.services.pcp_letter). Fabricated data; throwaway SQLite, no browser."""
import os
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ehr.models.care_coordination import OutsidePractitioner, PcpCommunication
from ehr.models.database import Base, Problem
from ehr.services import pcp_letter as svc


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory(prefix="npvehr-pcptest-") as tmp:
        engine = create_engine(f"sqlite:///{os.path.join(tmp, 't.db')}")
        Base.metadata.create_all(bind=engine)
        with Session(engine) as s:
            yield s
        engine.dispose()


def _exam(codes, dob="1960-05-01", pid=1, eid=10, dilated=None):
    return SimpleNamespace(id=eid, patient_id=pid, diagnosis_codes=codes, patient=SimpleNamespace(date_of_birth=dob),
                           dilated_exam_performed=dilated)


def test_code_parsing_gives_severity_and_edema_only_when_the_code_says_so():
    assert svc.retinopathy_codes("E11.3211") == [("E11.3211", "mild", "present")]
    assert svc.retinopathy_codes("E11.319, E10.3552") == [("E11.319", None, "absent"), ("E10.3552", "proliferative", None)]
    assert svc.retinopathy_codes("E11.9, H40.1131, E11.21") == []                     # diabetes without retinopathy never matches
    assert svc.retinopathy_codes("e11.3393") == [("E11.3393", "moderate", "absent")]


def test_suggestion_picks_most_severe_and_falls_back_to_the_problem_list(db):
    s = svc.suggestion(db, _exam("E11.3211 E11.3413"))
    assert s["severity"] == "severe" and s["edema"] == "present"
    db.add(Problem(patient_id=1, diagnosis_name="DR", icd10_code="E11.3293", status="Active")); db.commit()
    s = svc.suggestion(db, _exam("H25.9"))
    assert s["codes"] == ["E11.3293"] and s["severity"] == "mild" and s["edema"] == "absent"


def test_letter_is_due_only_for_adults_with_retinopathy_and_nothing_sent_or_excluded_in_12_months(db):
    now = datetime(2026, 6, 1)
    assert svc.candidate(db, _exam("E11.9"), now) is None                              # no retinopathy
    assert svc.candidate(db, _exam("E11.3211", dob="2015-01-01"), now) is None         # under 18
    assert svc.candidate(db, _exam("E11.3211", dob=None), now)["severity"] == "mild"   # unknown DOB: still prompts (advisory)
    due = svc.candidate(db, _exam("E11.3211"), now)
    assert due and due["draft"] is None
    d = PcpCommunication(patient_id=1, exam_id=10, status="drafted", created_at=now); db.add(d); db.commit()
    assert svc.candidate(db, _exam("E11.3211"), now)["draft"].id == d.id              # a draft doesn't satisfy it
    sent = PcpCommunication(patient_id=1, exam_id=10, status="sent", sent_at=now - timedelta(days=100), created_at=now - timedelta(days=100))
    db.add(sent); db.commit()
    assert svc.candidate(db, _exam("E11.3211"), now) is None                           # sent within a year
    assert svc.candidate(db, _exam("E11.3211", pid=2), now) is not None               # another patient unaffected
    assert svc.candidate(db, _exam("E11.3211"), now + timedelta(days=300)) is not None  # the window lapses
    db.delete(sent); db.add(PcpCommunication(patient_id=3, exam_id=11, status="excluded", exclusion_reason="patient_refusal", created_at=now)); db.commit()
    assert svc.candidate(db, _exam("E11.3211", pid=3, eid=11), now) is None           # a documented exclusion satisfies it too


def test_template_must_carry_severity_and_edema_and_only_known_placeholders():
    assert svc.template_error("Dear {pcp_name}: {severity}, {macular_edema}") is None
    assert "{macular_edema}" in svc.template_error("Dear {pcp_name}: {severity}")
    assert "{evil}" in svc.template_error("{severity} {macular_edema} {evil}")


def test_render_substitutes_only_whitelisted_placeholders():
    out = svc.render("Hi {pcp_name} {severity} {__class__} {unknown} {{x}}", {"pcp_name": "Dr. A", "severity": "Mild", "__class__": "boom", "unknown": "no"})
    assert out == "Hi Dr. A Mild {__class__} {unknown} {{x}}"


def test_directory_validation_helpers(db):
    assert svc.clean_fax("(717) 555-0100") == "7175550100" and svc.clean_fax("1-717-555-0100") == "7175550100" and svc.clean_fax("") is None
    assert svc.valid_fax("7175550100") and svc.valid_fax(None) and not svc.valid_fax("555")
    assert svc.valid_npi("1234567890") and svc.valid_npi("") and not svc.valid_npi("12345")
    p = OutsidePractitioner(first_name="Pat", last_name="Sample", credentials="MD", practice_name="Sample Family Care", fax="7175550100")
    assert svc.practitioner_label(p) == "Pat Sample, MD" and "fax 7175550100" in svc.recipient_snapshot(p)


def test_an_exam_recorded_as_not_dilated_never_prompts_but_unrecorded_and_dilated_do(db):
    now = datetime(2026, 6, 1)
    assert svc.candidate(db, _exam("E11.3211", dilated=False), now) is None            # recorded as not dilated: outside the measure
    assert svc.candidate(db, _exam("E11.3211", dilated=True), now) is not None
    assert svc.candidate(db, _exam("E11.3211", dilated=None), now) is not None         # not recorded (older exams): the clinician confirms on the letter

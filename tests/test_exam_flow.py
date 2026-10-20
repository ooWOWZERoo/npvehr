"""Unit tests for the exam flow's Rx hand-off (ehr.routes.prescriptions._prefill_from_exam): which values a new prescription opens with.
Throwaway SQLite file, no browser."""
import os
import tempfile
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ehr.models.database import Base, EyeExam, Patient, Provider, Refraction
from ehr.routes import prescriptions as rx_routes


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory(prefix="npvehr-examflow-") as tmp:
        engine = create_engine(f"sqlite:///{os.path.join(tmp, 't.db')}")
        Base.metadata.create_all(bind=engine)
        with Session(engine) as s:
            yield s
        engine.dispose()


def _exam(db, rx_decision="glasses", manifest=True, patient_name="Pat"):
    pat = Patient(first_name=patient_name, last_name="Tester", date_of_birth="1980-01-01")
    prov = Provider(first_name="Doc", last_name="Tor", license_number="L-1", npi="1")
    db.add_all([pat, prov]); db.flush()
    ex = EyeExam(patient_id=pat.id, provider_id=prov.id, exam_date="2026-10-20", rx_decision=rx_decision)
    db.add(ex); db.flush()
    db.add(Refraction(exam_id=ex.id, refraction_type="habitual", od_sphere=-1.0))
    db.add(Refraction(exam_id=ex.id, refraction_type="autorefraction", od_sphere=-9.0))
    if manifest:
        db.add(Refraction(exam_id=ex.id, refraction_type="manifest", od_sphere=-3.25, od_cylinder=-0.75, od_axis=180, od_add=1.5, os_sphere=-2.5))
    db.commit()
    return ex


def test_glasses_prescription_opens_with_the_manifest_values_only(db):
    ex = _exam(db)
    values, rx_type = rx_routes._prefill_from_exam(db, ex.id, ex.patient_id)
    assert rx_type == "glasses"
    assert values == {"od_sphere": -3.25, "od_cylinder": -0.75, "od_axis": 180, "od_add": 1.5, "os_sphere": -2.5}   # not habitual (-1.00) or auto (-9.00)


def test_a_decision_of_none_or_both_still_prefills_glasses_and_contacts_start_blank(db):
    for decision in (None, "none", "both"):
        ex = _exam(db, rx_decision=decision)
        assert rx_routes._prefill_from_exam(db, ex.id, ex.patient_id)[1] == "glasses"
        assert rx_routes._prefill_from_exam(db, ex.id, ex.patient_id)[0]["od_sphere"] == -3.25
    ex = _exam(db, rx_decision="contacts")
    assert rx_routes._prefill_from_exam(db, ex.id, ex.patient_id) == ({}, "contacts")        # spectacle values don't transfer to contact lenses


def test_nothing_is_copied_without_a_manifest_refraction_for_another_patient_or_for_an_unknown_exam(db):
    assert rx_routes._prefill_from_exam(db, _exam(db, manifest=False).id, None) == ({}, "glasses")
    ex = _exam(db)
    assert rx_routes._prefill_from_exam(db, ex.id, ex.patient_id + 1) == ({}, None)          # the exam is not this patient's
    assert rx_routes._prefill_from_exam(db, 999999, None) == ({}, None)
    assert rx_routes._prefill_from_exam(db, None, None) == ({}, None)


def test_record_level_authorization_is_respected(db, monkeypatch):
    ex = _exam(db)
    monkeypatch.setattr(rx_routes.authz, "can_view_patient", lambda d, user, pid: False)
    assert rx_routes._prefill_from_exam(db, ex.id, ex.patient_id, SimpleNamespace(id=1, role="optometrist_provider")) == ({}, None)
    monkeypatch.setattr(rx_routes.authz, "can_view_patient", lambda d, user, pid: True)
    assert rx_routes._prefill_from_exam(db, ex.id, ex.patient_id, SimpleNamespace(id=1, role="optometrist_provider"))[0]["od_sphere"] == -3.25

"""Unit tests for the recall worklist (ehr.services.recalls). Fabricated data; throwaway SQLite, no browser."""
import os
import tempfile
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ehr.models.database import Appointment, AppointmentStatus, Base, EyeExam, FieldChangeAuditEvent, Patient
from ehr.models.imports import PatientRecall, RecallAction
from ehr.services import recalls as svc

TODAY = date(2026, 6, 15)
ADMIN = SimpleNamespace(role="system_administrator", provider_id=None)


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory(prefix="npvehr-recalltest-") as tmp:
        engine = create_engine(f"sqlite:///{os.path.join(tmp, 't.db')}")
        Base.metadata.create_all(bind=engine)
        with Session(engine) as s:
            for i, (last, first) in enumerate([("Ames", "Al"), ("Bell", "Bo"), ("Cole", "Cy"), ("Dunn", "Di")], start=1):
                s.add(Patient(id=i, first_name=first, last_name=last, date_of_birth="1970-01-01", phone=f"610555010{i}"))
            s.commit()
            yield s
        engine.dispose()


def _recall(db, pid, due, rtype="12 Month Adult", status="open"):
    r = PatientRecall(patient_id=pid, recall_type=rtype, due_date=due, status=status)
    db.add(r); db.commit()
    return r


def _names(db, **kw):
    return [p.last_name for _, p in svc.worklist_query(db, ADMIN, today=TODAY, **kw).all()]


def test_windows_status_type_and_search_filters_and_ordering(db):
    _recall(db, 1, TODAY - timedelta(days=100))
    _recall(db, 2, TODAY - timedelta(days=5), "6 Months Child")
    _recall(db, 3, TODAY + timedelta(days=20))
    _recall(db, 4, TODAY + timedelta(days=60))
    _recall(db, 1, TODAY - timedelta(days=200), status="satisfied")
    assert _names(db) == ["Ames", "Bell", "Cole"]                               # default: overdue + next 30 days, open only, most overdue first
    assert _names(db, window="overdue") == ["Ames", "Bell"]
    assert _names(db, window="90") == ["Ames", "Bell", "Cole", "Dunn"]
    assert _names(db, window="all", status="satisfied") == ["Ames"]
    assert _names(db, window="all", status="all") == ["Ames", "Ames", "Bell", "Cole", "Dunn"]
    assert _names(db, rtype="6 Months Child") == ["Bell"]
    assert _names(db, q="co") == ["Cole"] and _names(db, q="5550101") == ["Ames"]
    c = svc.counts(db, ADMIN, today=TODAY)
    assert c == {"overdue": 2, "soon": 1, "open": 4}


def test_provider_sees_only_own_patients(db):
    _recall(db, 1, TODAY - timedelta(days=1)); _recall(db, 2, TODAY - timedelta(days=1))
    db.add(Appointment(patient_id=2, provider_id=9, scheduled_at=datetime(2025, 1, 1), status=AppointmentStatus.completed)); db.commit()
    provider = SimpleNamespace(role="optometrist_provider", provider_id=9)
    assert [p.last_name for _, p in svc.worklist_query(db, provider, today=TODAY).all()] == ["Bell"]
    nobody = SimpleNamespace(role="optometrist_provider", provider_id=None)
    assert svc.worklist_query(db, nobody, today=TODAY).all() == []                 # unlinked provider sees none (fail closed)


def test_context_booked_seen_and_contacts_and_hide_booked(db):
    r1 = _recall(db, 1, TODAY - timedelta(days=10)); r2 = _recall(db, 2, TODAY - timedelta(days=10)); r3 = _recall(db, 3, TODAY - timedelta(days=10))
    db.add(Appointment(patient_id=1, provider_id=1, scheduled_at=datetime.combine(TODAY + timedelta(days=3), datetime.min.time()), status=AppointmentStatus.scheduled))
    db.add(Appointment(patient_id=3, provider_id=1, scheduled_at=datetime.combine(TODAY + timedelta(days=3), datetime.min.time()), status=AppointmentStatus.cancelled))
    db.add(EyeExam(patient_id=2, provider_id=1, exam_date=(TODAY - timedelta(days=15)).isoformat()))   # near the due date: seen
    db.add(EyeExam(patient_id=3, provider_id=1, exam_date=(TODAY - timedelta(days=400)).isoformat()))  # long before: not seen
    db.commit()
    svc.log_contact(db, r2, 5, "phone", "left voicemail"); svc.log_contact(db, r2, 5, "email"); db.commit()
    rows = svc.worklist_query(db, ADMIN, today=TODAY).all()
    ctx = svc.context_for(db, rows, today=TODAY)
    assert ctx[r1.id]["booked"] is not None and ctx[r1.id]["seen"] is None
    assert ctx[r2.id]["seen"] == (TODAY - timedelta(days=15)).isoformat() and ctx[r2.id]["contacts"] == 2 and ctx[r2.id]["last_method"] == "email"
    assert ctx[r3.id]["booked"] is None and ctx[r3.id]["seen"] is None            # a cancelled appointment doesn't count
    assert _names(db, hide_booked=True) == ["Bell", "Cole"]


def test_contact_close_dismiss_reopen_rules_and_audit(db):
    r = _recall(db, 1, TODAY)
    assert svc.log_contact(db, r, 5, "carrier pigeon") is None and svc.log_contact(db, r, 5, "text").method == "text"
    assert svc.close(db, r, "dismissed", 5, "") == "Give a reason for dismissing a recall."
    assert svc.close(db, r, "dismissed", 5, "no") == "Give a reason for dismissing a recall."
    assert svc.close(db, r, "bogus", 5) == "Unknown action."
    assert svc.close(db, r, "satisfied", 5, "seen last week") is None and r.status == "satisfied"
    assert svc.close(db, r, "satisfied", 5) == "That recall is already closed."
    assert svc.reopen(db, r, 5) and r.status == "open" and not svc.reopen(db, r, 5)
    assert svc.close(db, r, "dismissed", 5, "moved away") is None and r.status == "dismissed"
    db.commit()
    assert [a.action for a in db.query(RecallAction).order_by(RecallAction.id)] == ["contacted", "satisfied", "reopened", "dismissed"]
    assert db.query(FieldChangeAuditEvent).filter_by(table_name="patient_recalls", record_id=r.id).count() == 3

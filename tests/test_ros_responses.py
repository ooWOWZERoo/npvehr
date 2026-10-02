"""Unit tests for ehr.services.ros_responses (ROS plan stage 4): only valid, active catalog ids are
recorded, input is deduplicated and bounded, and a failure inside the ROS write can never take the
caller's transaction (the exam save) down with it. Throwaway SQLite file, no browser."""
import os
import tempfile

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from ehr.models.database import Base
from ehr.models.ros import EncounterRosResponse, RosMaster
from ehr.services import ros_responses


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory(prefix="npvehr-rostest-") as tmp:
        engine = create_engine(f"sqlite:///{os.path.join(tmp, 't.db')}")
        Base.metadata.create_all(bind=engine)
        with Session(engine) as session:
            session.add_all([
                RosMaster(body_system="Endocrine", prompt_text="History of diabetes", sort_order=10, is_active=True),
                RosMaster(body_system="Cardiovascular", prompt_text="High blood pressure", sort_order=10, is_active=True),
                RosMaster(body_system="Ocular", prompt_text="Retired prompt", sort_order=10, is_active=False),
            ])
            session.commit()
            yield session
        engine.dispose()


def _ids(db):
    return {r.prompt_text: r.id for r in db.query(RosMaster).all()}


def test_records_only_valid_active_deduplicated_positive_findings(db):
    ids = _ids(db)
    raw = [ids["History of diabetes"], str(ids["History of diabetes"]), ids["High blood pressure"],
           ids["Retired prompt"], 999999, "abc", "", "-4", None]
    assert ros_responses.record_findings(db, 1, raw) == 2
    db.commit()
    rows = db.query(EncounterRosResponse).filter(EncounterRosResponse.exam_id == 1).all()
    assert {r.ros_item_id for r in rows} == {ids["History of diabetes"], ids["High blood pressure"]}
    assert all(r.status == "positive" for r in rows)
    assert dict(ros_responses.findings_for_exam(db, 1)) == {"Endocrine": ["History of diabetes"],
                                                            "Cardiovascular": ["High blood pressure"]}


def test_nothing_to_record_is_a_no_op_and_the_input_is_bounded(db):
    assert ros_responses.record_findings(db, 1, []) == 0
    assert ros_responses.record_findings(db, 1, ["x"] * 5) == 0
    assert db.query(EncounterRosResponse).count() == 0
    many = list(range(1, 10_000))                       # a hostile request can't fan out into thousands of rows/params
    assert len(ros_responses._parse_ids(many)) == ros_responses.MAX_FINDINGS


def test_a_failure_in_the_ros_write_never_breaks_the_callers_transaction(db):
    ids = _ids(db)
    db.execute(text("CREATE TABLE exam_sentinel (id INTEGER PRIMARY KEY)"))
    db.execute(text("INSERT INTO exam_sentinel (id) VALUES (1)"))            # stands in for the exam row
    db.execute(text("DROP TABLE encounter_ros_responses"))                    # make the ROS write blow up
    assert ros_responses.record_findings(db, 1, [ids["History of diabetes"]]) == 0
    assert ros_responses.findings_for_exam(db, 1) == []                      # reads degrade to empty too
    db.commit()
    assert db.execute(text("SELECT COUNT(*) FROM exam_sentinel")).scalar() == 1   # the "exam" survived

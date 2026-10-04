"""Unit tests for the ROS catalog bulk import (ehr.services.ros_import). Fabricated sample data only."""
import os
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ehr.models.database import Base, FieldChangeAuditEvent
from ehr.models.imports import DataImportBatch, DataImportBatchRecord
from ehr.models.ros import RosIcd10TestMapping, RosMaster
from ehr.services import ros_import as svc

SAMPLE = (Path(__file__).parent / "fixtures" / "ros_import_sample.csv").read_bytes()


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory(prefix="npvehr-rosimport-") as tmp:
        engine = create_engine(f"sqlite:///{os.path.join(tmp, 't.db')}")
        Base.metadata.create_all(bind=engine)
        with Session(engine) as s:
            yield s
        engine.dispose()


def _apply(db):
    plan = svc.build_plan(db, svc.read_rows("x.csv", SAMPLE))
    b = DataImportBatch(kind=svc.KIND, status="staged"); db.add(b); db.flush()
    svc.apply_plan(db, plan, b, 1)
    b.status = "imported"; db.commit()
    return plan, b


def test_plan_counts_and_skip_reasons(db):
    plan = svc.build_plan(db, svc.read_rows("x.csv", SAMPLE))
    assert plan.rows_total == 9
    assert len(plan.new_items) == 4 and plan.new_rules == 3          # thyroid(2 rules), no-rule prompt, hypertension(1), formula prompt
    assert plan.repeats_in_file == 1
    reasons = [r for _, r in plan.skipped]
    assert len(reasons) == 3 and any("13 ROS systems" in r for r in reasons) and any("both an ICD-10 and a CPT" in r for r in reasons) \
        and any("CPT code isn't a valid" in r for r in reasons)


def test_apply_lands_unreviewed_with_note_source_and_audit_and_is_idempotent(db):
    _apply(db)
    m = db.query(RosIcd10TestMapping).filter_by(suggested_icd10="E05.90", recommended_cpt="92250").one()
    assert m.reviewed is False and m.source_citation is None and m.reviewed_by_user_id is None
    assert m.icd10_pattern == "E05" and "Source (not yet verified): Sample LCD" in m.compliance_rule
    assert db.query(RosMaster).filter_by(body_system="Endocrine", prompt_text="Sample thyroid history").count() == 1
    assert len({e.record_id for e in db.query(FieldChangeAuditEvent).filter_by(table_name="ros_icd10_test_mapping")}) == 3
    again = svc.build_plan(db, svc.read_rows("x.csv", SAMPLE))
    assert not again.new_items and again.new_rules == 0 and again.rules_already_on_file == 4


def test_existing_prompt_is_never_changed_but_gets_new_rules(db):
    item = RosMaster(body_system="Endocrine", prompt_text="Sample Thyroid History", sort_order=10, is_active=False)
    db.add(item); db.commit()
    plan = svc.build_plan(db, svc.read_rows("x.csv", SAMPLE))
    assert plan.existing_items == 1 and len(plan.new_items) == 3
    _apply(db)
    db.refresh(item)
    assert item.is_active is False and item.prompt_text == "Sample Thyroid History" and len(item.mappings) == 2


def test_undo_keeps_reviewed_rules_and_prompts_in_use(db):
    _, b = _apply(db)
    m = db.query(RosIcd10TestMapping).filter_by(recommended_cpt="92134").one()
    m.reviewed, m.source_citation = True, "Sample LCD"
    db.commit()
    removed, kept, items_removed, items_kept = svc.undo_batch(db, b, 1)
    db.commit()
    assert (removed, kept) == (2, 1)                                  # the reviewed rule survives
    assert items_removed == 3 and items_kept == 1                     # its prompt survives; the other 3 go
    assert db.query(DataImportBatchRecord).count() == 0 and b.status == "undone"
    assert db.query(RosIcd10TestMapping).count() == 1


def test_export_round_trips_and_neutralises_formulas(db):
    _apply(db)
    out = svc.export_csv(db)
    assert "'=Sample formula prompt" in out                          # exported with a guard against spreadsheet formulas
    plan = svc.build_plan(db, svc.read_rows("e.csv", out.encode()))
    assert not plan.new_items and plan.new_rules == 0 and not plan.skipped    # and re-imports as exactly what is on file


def test_header_errors_and_template():
    with pytest.raises(svc.ImportFileError):
        svc.read_rows("x.csv", b"foo,bar\n1,2\n")
    assert svc.read_rows("t.csv", svc.template_csv().encode())[0]["system"] == "Endocrine"

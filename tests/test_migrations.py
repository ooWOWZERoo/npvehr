"""Automated migration test suite (spec §18.3 item 7 / §36.5 item 4
follow-up): every migration round up to this one was verified manually --
boot a fresh SQLite database, confirm every migration applies cleanly, then
re-run the exact same sequence and confirm it's a safe no-op. This makes
that same check a standing, automatic regression test instead of a
per-round manual ritual (still worth doing by hand for anything genuinely
new/risky, but no longer the only safety net).

Unit-level: a throwaway file-backed SQLite database via a fresh
`sqlalchemy.create_engine(...)`, not the app's own live_server/Playwright
fixtures -- no browser needed, and no interference with (or dependency on)
the app's own DATABASE_URL-bound `engine`.
"""
import os
import tempfile

import pytest
from sqlalchemy import create_engine, inspect, text

from ehr.db import migrations as mig
from ehr.models.database import Base


@pytest.fixture
def fresh_engine():
    with tempfile.TemporaryDirectory(prefix="npvehr-migtest-") as tmp_dir:
        db_path = os.path.join(tmp_dir, "test.db")
        engine = create_engine(f"sqlite:///{db_path}")
        yield engine
        engine.dispose()


def _run_all_migrations(engine):
    """Mirrors the documented run order (ehr/app.py's startup hook, and
    ehr/db/seed.py's seed() CLI entrypoint): column migrations on
    pre-existing tables, then create_all() for brand-new tables, then
    data-seeding migrations that need those new tables to already exist."""
    mig.run_column_migrations(engine)
    Base.metadata.create_all(bind=engine)
    mig.run_post_create_all_migrations(engine)


def test_fresh_database_migration_boot_is_clean(fresh_engine):
    """Every migration function runs without error against a brand-new
    database, and every registered migration id ends up recorded in
    schema_migrations (nothing silently skipped)."""
    _run_all_migrations(fresh_engine)
    with fresh_engine.connect() as conn:
        applied = {row[0] for row in conn.execute(text("SELECT id FROM schema_migrations")).fetchall()}
    expected = {mid for mid, _ in mig.COLUMN_MIGRATIONS} | {mid for mid, _ in mig.POST_CREATE_ALL_MIGRATIONS}
    assert expected.issubset(applied)


def test_migration_rerun_against_already_migrated_database_is_idempotent(fresh_engine):
    """Running every migration a second time against an already-migrated
    database must be a safe no-op: the exact "idempotent re-run" check this
    app's build process has always done manually before this round --
    confirmed here by asserting the full column set and row count of every
    table are byte-for-byte identical before and after the second pass."""
    _run_all_migrations(fresh_engine)

    inspector = inspect(fresh_engine)
    tables = inspector.get_table_names()
    before_columns = {t: {c["name"] for c in inspector.get_columns(t)} for t in tables}
    with fresh_engine.connect() as conn:
        before_counts = {t: conn.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar() for t in tables}

    _run_all_migrations(fresh_engine)

    inspector = inspect(fresh_engine)
    after_columns = {t: {c["name"] for c in inspector.get_columns(t)} for t in inspector.get_table_names()}
    with fresh_engine.connect() as conn:
        after_counts = {t: conn.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar() for t in after_columns}
    assert after_columns == before_columns
    assert after_counts == before_counts


def test_every_registered_migration_id_is_unique():
    """A duplicate migration id would silently shadow one migration under
    _applied()'s id-keyed lookup, so the second one would never actually
    run on a fresh database -- guard against that copy/paste mistake
    directly, independent of any database."""
    ids = [mid for mid, _ in mig.COLUMN_MIGRATIONS] + [mid for mid, _ in mig.POST_CREATE_ALL_MIGRATIONS]
    assert len(ids) == len(set(ids)), "duplicate migration id registered"


def test_pk_ddl_and_dialect_detection_for_both_supported_databases():
    """_pk_ddl/_is_postgres are the one place migration DDL genuinely
    branches by database dialect (SQLite AUTOINCREMENT vs. Postgres SERIAL)
    -- this app runs on SQLite locally/in CI and Postgres (Neon) in deployed
    environments, but only the SQLite path was ever actually exercised by a
    test. A real Postgres instance isn't available here, but the branch
    itself is a two-line pure function keyed only on conn.engine.dialect.name,
    so a lightweight fake stands in for both dialects without needing one."""
    class _FakeDialect:
        def __init__(self, name):
            self.name = name

    class _FakeEngine:
        def __init__(self, name):
            self.dialect = _FakeDialect(name)

    class _FakeConn:
        def __init__(self, name):
            self.engine = _FakeEngine(name)

    sqlite_conn = _FakeConn("sqlite")
    pg_conn = _FakeConn("postgresql")
    assert mig._is_postgres(sqlite_conn) is False
    assert mig._is_postgres(pg_conn) is True
    assert mig._pk_ddl(sqlite_conn) == "INTEGER PRIMARY KEY AUTOINCREMENT"
    assert mig._pk_ddl(pg_conn) == "SERIAL PRIMARY KEY"


def test_rollback_reverses_a_column_migration_and_is_reapplyable(fresh_engine):
    """Down-migration/rollback capability (spec §36.5 item 4 / §25.15
    follow-up): the runner previously only ever added, never reversed.
    A registered column-migration rollback drops the column and un-records
    it from schema_migrations; running the migrations again cleanly
    re-applies it."""
    _run_all_migrations(fresh_engine)
    inspector = inspect(fresh_engine)
    assert "provider_id" in {c["name"] for c in inspector.get_columns("users")}

    mig.rollback_migration(fresh_engine, "045_user_provider_link")
    inspector = inspect(fresh_engine)
    assert "provider_id" not in {c["name"] for c in inspector.get_columns("users")}
    with fresh_engine.connect() as conn:
        applied = {row[0] for row in conn.execute(text("SELECT id FROM schema_migrations")).fetchall()}
    assert "045_user_provider_link" not in applied

    _run_all_migrations(fresh_engine)
    inspector = inspect(fresh_engine)
    assert "provider_id" in {c["name"] for c in inspector.get_columns("users")}
    with fresh_engine.connect() as conn:
        applied = {row[0] for row in conn.execute(text("SELECT id FROM schema_migrations")).fetchall()}
    assert "045_user_provider_link" in applied


def test_rollback_of_table_creation_migration_drops_the_table(fresh_engine):
    """A registered table-creation migration's rollback drops the whole
    table, not just un-records it."""
    _run_all_migrations(fresh_engine)
    inspector = inspect(fresh_engine)
    assert "rx_lab_orders" in inspector.get_table_names()

    mig.rollback_migration(fresh_engine, "043_create_rx_lab_orders")
    inspector = inspect(fresh_engine)
    assert "rx_lab_orders" not in inspector.get_table_names()


def test_rollback_rejects_unknown_or_unregistered_or_unapplied_migrations(fresh_engine):
    """Reversibility is opt-in per migration (most migrations have no
    down-migration at all) -- rollback_migration must reject an unknown id,
    a real id with no registered down-migration, and a real registered id
    that isn't currently applied, rather than guessing or silently no-oping."""
    with pytest.raises(ValueError):
        mig.rollback_migration(fresh_engine, "999_not_a_real_migration")

    # Real migration, but not one of the (deliberately sparse) reversible ones.
    with pytest.raises(ValueError):
        mig.rollback_migration(fresh_engine, "001_appointment_columns")

    # Real, reversible migration -- but nothing has been applied to this
    # engine yet, so there's nothing to roll back.
    with pytest.raises(ValueError):
        mig.rollback_migration(fresh_engine, "045_user_provider_link")


def test_glaucoma_oct_onh_backfill_only_recodes_exam_linked_orders(fresh_engine):
    """migration_050's backfill must recode only DiagnosticOrder rows created
    via the Glaucoma dashboard's "OCT RNFL" checkbox (ordered_exam_id set) --
    a quick-ordered "OCT" row from a look-back alert (ordered_exam_id NULL)
    could have come from the glaucoma, AMD, or diabetic-retinopathy profile,
    all of which used to share the generic "OCT" code, so it must be left
    untouched rather than guessed at."""
    _run_all_migrations(fresh_engine)
    with fresh_engine.begin() as conn:
        oct_id = conn.execute(text("SELECT id FROM diagnostic_tests WHERE code = 'OCT'")).fetchone()[0]
        oct_onh_id = conn.execute(text("SELECT id FROM diagnostic_tests WHERE code = 'OCT_ONH'")).fetchone()[0]
        conn.execute(text("""
            INSERT INTO patients (first_name, last_name, sms_opt_in, email_opt_in)
            VALUES ('Backfill', 'Testpatient', 0, 0)
        """))
        patient_id = conn.execute(text("SELECT id FROM patients WHERE last_name = 'Testpatient'")).fetchone()[0]
        conn.execute(text("""
            INSERT INTO diagnostic_orders (patient_id, diagnostic_test_id, ordered_exam_id, status)
            VALUES (:pid, :oct, 1, 'ordered')
        """), {"pid": patient_id, "oct": oct_id})
        conn.execute(text("""
            INSERT INTO diagnostic_orders (patient_id, diagnostic_test_id, ordered_exam_id, status)
            VALUES (:pid, :oct, NULL, 'ordered')
        """), {"pid": patient_id, "oct": oct_id})

    with fresh_engine.begin() as conn:
        mig.migration_050_backfill_glaucoma_oct_onh_orders(conn)

    with fresh_engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT diagnostic_test_id, ordered_exam_id FROM diagnostic_orders ORDER BY id
        """)).fetchall()
    assert rows[0] == (oct_onh_id, 1)  # exam-linked -- recoded
    assert rows[1] == (oct_id, None)  # quick-ordered -- left alone


def test_followup_per_visit_backfill_migrates_active_recommendation_only(fresh_engine):
    """migration_052's backfill (return-visit recommendation carry-forward,
    round 2 -- "allow for up to two return visit planning") must migrate an
    exam's active (pending/scheduled) legacy follow_up_weeks recommendation
    into a new EyeExamFollowUp row, re-point that exam's already-created
    DiagnosticOrder at it via follow_up_id, and leave a dismissed (or
    weeks-less) exam alone entirely -- a dismissed recommendation predates
    this feature under the old single-recommendation model and shouldn't
    resurrect itself as a new pending row."""
    _run_all_migrations(fresh_engine)
    with fresh_engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO patients (first_name, last_name, sms_opt_in, email_opt_in)
            VALUES ('Followup', 'Backfill', 0, 0)
        """))
        patient_id = conn.execute(text("SELECT id FROM patients WHERE last_name = 'Backfill'")).fetchone()[0]
        conn.execute(text("INSERT INTO providers (first_name, last_name, active) VALUES ('Dr', 'Test', 1)"))
        provider_id = conn.execute(text("SELECT id FROM providers WHERE last_name = 'Test'")).fetchone()[0]
        vf_id = conn.execute(text("SELECT id FROM diagnostic_tests WHERE code = 'VF'")).fetchone()[0]

        # Exam A: an active (pending) legacy recommendation -- should migrate.
        conn.execute(text("""
            INSERT INTO eye_exams (patient_id, provider_id, exam_date, follow_up_weeks, follow_up_unit,
                                    follow_up_reason, follow_up_status)
            VALUES (:p, :pr, '2026-01-01', 6, 'Week', 'Recheck VF', 'pending')
        """), {"p": patient_id, "pr": provider_id})
        exam_a_id = conn.execute(text(
            "SELECT id FROM eye_exams WHERE follow_up_status = 'pending'")).fetchone()[0]
        conn.execute(text("""
            INSERT INTO diagnostic_orders (patient_id, diagnostic_test_id, ordered_exam_id, status)
            VALUES (:p, :t, :e, 'ordered')
        """), {"p": patient_id, "t": vf_id, "e": exam_a_id})

        # Exam B: already dismissed -- should NOT migrate (migration 051
        # already backfilled every pre-existing exam to 'dismissed').
        conn.execute(text("""
            INSERT INTO eye_exams (patient_id, provider_id, exam_date, follow_up_weeks, follow_up_unit,
                                    follow_up_reason, follow_up_status)
            VALUES (:p, :pr, '2026-01-02', 3, 'Month', 'Old data', 'dismissed')
        """), {"p": patient_id, "pr": provider_id})

    with fresh_engine.begin() as conn:
        mig.migration_052_followup_per_visit(conn)

    with fresh_engine.connect() as conn:
        followup_id, eye_exam_id, weeks, reason, status = conn.execute(text(
            "SELECT id, eye_exam_id, follow_up_weeks, reason, status FROM eye_exam_followups")).fetchone()
        followup_count = conn.execute(text("SELECT COUNT(*) FROM eye_exam_followups")).scalar_one()
        order_followup_id = conn.execute(text("SELECT follow_up_id FROM diagnostic_orders")).fetchone()[0]
    assert followup_count == 1  # exam B never got a row
    assert (eye_exam_id, weeks, reason, status) == (exam_a_id, 6, "Recheck VF", "pending")
    assert order_followup_id == followup_id


def test_ros_catalog_seed_is_idempotent_and_starter_rules_are_unreviewed(fresh_engine):
    """Migration 055 seeds the 13-system ROS prompt catalog and four starter rules.
    Every starter rule must be UNREVIEWED (suggestion only -- none may ever raise a
    compliance advisory without an administrator's sign-off), and re-running the
    seed must not duplicate anything."""
    _run_all_migrations(fresh_engine)
    with fresh_engine.connect() as conn:
        systems = {r[0] for r in conn.execute(text("SELECT DISTINCT body_system FROM ros_master")).fetchall()}
        n_items = conn.execute(text("SELECT COUNT(*) FROM ros_master")).scalar()
        n_rules = conn.execute(text("SELECT COUNT(*) FROM ros_icd10_test_mapping")).scalar()
        reviewed = conn.execute(text("SELECT COUNT(*) FROM ros_icd10_test_mapping WHERE reviewed = 1")).scalar()
    assert len(systems) == 13
    assert n_rules == 4 and reviewed == 0

    with fresh_engine.begin() as conn:
        mig.migration_055_seed_ros_catalog(conn)       # direct re-run, bypassing the applied-ids ledger
    with fresh_engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM ros_master")).scalar() == n_items
        assert conn.execute(text("SELECT COUNT(*) FROM ros_icd10_test_mapping")).scalar() == n_rules


def test_safety_flag_type_seed_has_no_rules_and_is_idempotent(fresh_engine):
    """Migration 056 seeds only the neutral patient-status questions. It must not ship a single warning rule
    (the practice's clinicians write and sign those off), and re-running it must not duplicate anything."""
    _run_all_migrations(fresh_engine)
    with fresh_engine.connect() as conn:
        codes = {r[0] for r in conn.execute(text("SELECT code FROM safety_flag_types")).fetchall()}
        assert codes == {"pregnant", "nursing", "blood_thinner", "narrow_angle"}
        assert conn.execute(text("SELECT COUNT(*) FROM safety_rules")).scalar() == 0
    with fresh_engine.begin() as conn:
        mig.migration_056_seed_safety_flag_types(conn)
    with fresh_engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM safety_flag_types")).scalar() == 4


def test_pcp_letter_template_seed_is_idempotent_and_never_overwrites_an_edit(fresh_engine):
    """Migration 057 seeds one neutral default letter body; re-running it must not duplicate it or undo an edit."""
    _run_all_migrations(fresh_engine)
    with fresh_engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM communication_templates")).scalar() == 1
        conn.execute(text("UPDATE communication_templates SET body = 'edited {severity} {macular_edema}'"))
    with fresh_engine.begin() as conn:
        mig.migration_057_seed_pcp_letter_template(conn)
    with fresh_engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM communication_templates")).scalar() == 1
        assert conn.execute(text("SELECT body FROM communication_templates")).scalar() == "edited {severity} {macular_edema}"


def test_recall_status_column_is_added_to_an_existing_table_and_rerun_is_safe(fresh_engine):
    """Migration 058: a patient_recalls table from before the worklist (no status column) gains it with every row 'open'."""
    with fresh_engine.begin() as conn:
        conn.execute(text("CREATE TABLE patient_recalls (id INTEGER PRIMARY KEY, patient_id INTEGER, recall_type VARCHAR(80), due_date DATE)"))
        conn.execute(text("INSERT INTO patient_recalls (patient_id, recall_type, due_date) VALUES (1, '12 Month Adult', '2026-01-01')"))
    with fresh_engine.begin() as conn:
        mig.migration_058_recall_status(conn)
    with fresh_engine.begin() as conn:
        mig.migration_058_recall_status(conn)                                  # re-run is a no-op
    with fresh_engine.connect() as conn:
        assert conn.execute(text("SELECT status FROM patient_recalls")).scalar() == "open"


def test_dilated_exam_columns_are_added_to_an_existing_eye_exams_table_and_rerun_is_safe(fresh_engine):
    """Migration 059: an eye_exams table from before the dilation field gains both columns, existing rows read NULL (not recorded)."""
    with fresh_engine.begin() as conn:
        conn.execute(text("CREATE TABLE eye_exams (id INTEGER PRIMARY KEY, patient_id INTEGER)"))
        conn.execute(text("INSERT INTO eye_exams (patient_id) VALUES (1)"))
    for _ in range(2):                                                         # second run is a no-op
        with fresh_engine.begin() as conn:
            mig.migration_059_dilated_exam(conn)
    with fresh_engine.connect() as conn:
        row = conn.execute(text("SELECT dilated_exam_performed, dilation_note FROM eye_exams")).one()
        assert tuple(row) == (None, None)


def test_medication_catalog_columns_and_frequency_seed_are_safe_on_existing_data_and_rerun(fresh_engine):
    """Migrations 060/061: an old patient_medications table gains five nullable/defaulted columns without touching its rows,
    the frequency pick list seeds once, and a re-run (or an administrator's own row) is never duplicated or overwritten."""
    Base.metadata.create_all(fresh_engine, tables=[t for n, t in Base.metadata.tables.items() if n in ("drug_catalog", "drug_dosages", "drug_frequencies")])
    with fresh_engine.begin() as conn:
        conn.execute(text("CREATE TABLE patient_medications (id INTEGER PRIMARY KEY, patient_id INTEGER, name VARCHAR(160), strength VARCHAR(80), frequency VARCHAR(80), eye VARCHAR(2))"))
        conn.execute(text("INSERT INTO patient_medications (patient_id, name, strength, frequency, eye) VALUES (1, 'Old free-text drug', '5 mg', 'daily', 'OU')"))
    for _ in range(2):
        with fresh_engine.begin() as conn:
            mig.migration_060_medication_catalog_columns(conn)
    with fresh_engine.connect() as conn:
        row = conn.execute(text("SELECT name, strength, frequency, eye, drug_id, dosage_id, frequency_id, drug_rxcui, is_custom FROM patient_medications")).one()
        assert tuple(row) == ("Old free-text drug", "5 mg", "daily", "OU", None, None, None, None, 0)
    with fresh_engine.begin() as conn:                                        # a real FK: a bad catalog id is rejected, not stored
        conn.execute(text("PRAGMA foreign_keys=ON"))
        with pytest.raises(Exception):
            conn.execute(text("UPDATE patient_medications SET drug_id = 424242"))

    with fresh_engine.begin() as conn:
        conn.execute(text("INSERT INTO drug_frequencies (route_group, label, sort_order, is_active) VALUES ('oral', 'Our own wording', 1, 1)"))
        mig.migration_061_seed_drug_frequencies(conn)
        mig.migration_061_seed_drug_frequencies(conn)
    with fresh_engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM drug_frequencies")).scalar()
        assert conn.execute(text("SELECT COUNT(*) FROM (SELECT route_group, label FROM drug_frequencies GROUP BY 1, 2 HAVING COUNT(*) > 1)")).scalar() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM drug_frequencies WHERE label = 'Our own wording'")).scalar() == 1
    assert n > 20


def test_drug_catalog_resolved_at_column_is_added_to_an_existing_table_and_rerun_is_safe(fresh_engine):
    """Migration 062: a drug_catalog table from before the RxNorm loader gains resolved_at (NULL on existing rows); re-run is a no-op."""
    with fresh_engine.begin() as conn:
        conn.execute(text("CREATE TABLE drug_catalog (id INTEGER PRIMARY KEY, name VARCHAR(160))"))
        conn.execute(text("INSERT INTO drug_catalog (name) VALUES ('Hand-entered drug')"))
    for _ in range(2):
        with fresh_engine.begin() as conn:
            mig.migration_062_drug_catalog_resolved_at(conn)
    with fresh_engine.connect() as conn:
        assert tuple(conn.execute(text("SELECT name, resolved_at FROM drug_catalog")).one()) == ("Hand-entered drug", None)
    with fresh_engine.begin() as conn:                              # a brand-new database (no table yet) is skipped, not an error
        conn.execute(text("DROP TABLE drug_catalog"))
        mig.migration_062_drug_catalog_resolved_at(conn)


def test_drug_catalog_generic_checked_at_column_is_added_to_an_existing_table_and_rerun_is_safe(fresh_engine):
    """Migration 063: a drug_catalog table from before brand mapping gains generic_checked_at (NULL on existing rows); re-run is a no-op."""
    with fresh_engine.begin() as conn:
        conn.execute(text("CREATE TABLE drug_catalog (id INTEGER PRIMARY KEY, name VARCHAR(160))"))
        conn.execute(text("INSERT INTO drug_catalog (name) VALUES ('Some drug')"))
    for _ in range(2):
        with fresh_engine.begin() as conn:
            mig.migration_063_drug_catalog_generic_checked_at(conn)
    with fresh_engine.connect() as conn:
        assert tuple(conn.execute(text("SELECT name, generic_checked_at FROM drug_catalog")).one()) == ("Some drug", None)
    with fresh_engine.begin() as conn:
        conn.execute(text("DROP TABLE drug_catalog"))
        mig.migration_063_drug_catalog_generic_checked_at(conn)         # no table yet: skipped, not an error


def test_exam_pretesting_and_disposition_columns_are_added_to_an_existing_table_and_rerun_is_safe(fresh_engine):
    """Migration 064: an eye_exams table from before keratometry/PD/disposition gains the columns, existing rows read NULL; re-run is a no-op."""
    with fresh_engine.begin() as conn:
        conn.execute(text("CREATE TABLE eye_exams (id INTEGER PRIMARY KEY, patient_id INTEGER)"))
        conn.execute(text("INSERT INTO eye_exams (patient_id) VALUES (1)"))
    for _ in range(2):
        with fresh_engine.begin() as conn:
            mig.migration_064_exam_pretesting_and_disposition(conn)
    with fresh_engine.connect() as conn:
        row = conn.execute(text("SELECT k_od_flat, k_os_axis, pd_distance, pd_near, rx_decision, treatment_note, referral_note, education_note FROM eye_exams")).one()
        assert tuple(row) == (None,) * 8
    with fresh_engine.begin() as conn:
        conn.execute(text("DROP TABLE eye_exams"))
        mig.migration_064_exam_pretesting_and_disposition(conn)          # no table yet: skipped, not an error

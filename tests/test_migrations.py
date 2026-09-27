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

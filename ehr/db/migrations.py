"""Lightweight, idempotent, versioned migration runner.

Alembic is intentionally not used here -- this whole application is a single
self-contained script, and a full migration framework would be disproportionate.
Instead each migration is a small Python function that:
  1. Checks current schema state via sqlalchemy.inspect (dialect-agnostic).
  2. Applies raw ALTER TABLE / CREATE TABLE / data SQL only if not already applied.
  3. Is safe to run every time the app starts (idempotent), and is recorded by id
     in the `schema_migrations` table so it never re-runs.

Runs against both SQLite (local dev, DATABASE_URL unset) and Postgres (e.g. Neon,
in deployed environments) -- see _is_postgres()/_pk_ddl() below for the handful of
places DDL genuinely differs between the two.

Run order: add columns to existing tables -> create_all() for brand-new tables
(called by the caller in between) -> data seed migrations -> legacy backfill.
"""
from datetime import datetime
from sqlalchemy import text, inspect

def _is_postgres(conn) -> bool:
    return conn.engine.dialect.name == "postgresql"

def _pk_ddl(conn) -> str:
    """Autoincrementing integer primary key fragment, dialect-appropriate.
    SQLite: AUTOINCREMENT keyword. Postgres: SERIAL (no AUTOINCREMENT keyword)."""
    return "SERIAL PRIMARY KEY" if _is_postgres(conn) else "INTEGER PRIMARY KEY AUTOINCREMENT"

def _ensure_migrations_table(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id TEXT PRIMARY KEY,
            applied_at TEXT
        )
    """))

def _applied(conn, migration_id: str) -> bool:
    row = conn.execute(text("SELECT 1 FROM schema_migrations WHERE id = :id"), {"id": migration_id}).fetchone()
    return row is not None

def _mark_applied(conn, migration_id: str):
    conn.execute(text("INSERT INTO schema_migrations (id, applied_at) VALUES (:id, :ts)"),
                 {"id": migration_id, "ts": datetime.utcnow().isoformat()})

def _table_exists(conn, table_name: str) -> bool:
    # sqlalchemy.inspect works against either dialect (sqlite_master / pg_catalog
    # are both abstracted away), unlike the raw sqlite_master query this replaced.
    return inspect(conn).has_table(table_name)

def _existing_columns(conn, table_name: str):
    return {c["name"] for c in inspect(conn).get_columns(table_name)}

def _add_column_if_missing(conn, table_name: str, column_name: str, ddl_type_and_default: str):
    # ddl_type_and_default must be dialect-neutral SQL: use TIMESTAMP (not SQLite's
    # DATETIME) and TRUE/FALSE (not 0/1) for booleans -- both read fine on SQLite
    # (which has flexible type affinity and, since 3.23, TRUE/FALSE keywords) and
    # on Postgres (which requires them).
    cols = _existing_columns(conn, table_name)
    if column_name not in cols:
        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl_type_and_default}"))

# ---------------------------------------------------------------------------
# Migration: 001 -- add Appointment Scheduling Module columns to `appointments`
# ---------------------------------------------------------------------------
def migration_001_appointment_columns(conn):
    if not _table_exists(conn, "appointments"):
        return  # brand-new database; create_all() will create the full table with these columns.
    new_columns = [
        ("appointment_type_version_id", "INTEGER"),
        ("patient_relationship_at_booking", "VARCHAR DEFAULT 'established'"),
        ("patient_relationship_source", "VARCHAR DEFAULT 'automatic'"),
        ("patient_relationship_override_reason", "TEXT"),
        ("is_follow_up", "BOOLEAN DEFAULT FALSE"),
        ("scheduled_end_at", "TIMESTAMP"),
        ("buffer_before_minutes", "INTEGER DEFAULT 0"),
        ("buffer_after_minutes", "INTEGER DEFAULT 0"),
        ("arrival_lead_minutes", "INTEGER DEFAULT 0"),
        ("resolved_color", "VARCHAR"),
        ("resolved_color_reason", "VARCHAR"),
        ("duration_overridden", "BOOLEAN DEFAULT FALSE"),
        ("duration_override_reason", "TEXT"),
        ("conflict_overridden", "BOOLEAN DEFAULT FALSE"),
        ("conflict_override_reason", "TEXT"),
        ("updated_at", "TIMESTAMP"),
    ]
    for col, ddl in new_columns:
        _add_column_if_missing(conn, "appointments", col, ddl)
    # Backfill scheduled_end_at from legacy duration_minutes where still null.
    # SQLite's datetime() and Postgres's interval arithmetic aren't
    # cross-compatible, so this one statement is dialect-branched.
    if _is_postgres(conn):
        conn.execute(text("""
            UPDATE appointments
            SET scheduled_end_at = scheduled_at + (COALESCE(duration_minutes, 30) || ' minutes')::interval
            WHERE scheduled_end_at IS NULL
        """))
    else:
        conn.execute(text("""
            UPDATE appointments
            SET scheduled_end_at = datetime(scheduled_at, '+' || COALESCE(duration_minutes, 30) || ' minutes')
            WHERE scheduled_end_at IS NULL
        """))

SEED_COLOR = {
    "red": "#DC2626", "teal": "#0F766E", "dark_blue": "#1E3A5F",
    "dark_purple": "#6B21A8", "light_purple": "#C4B5FD", "pink": "#DB2777",
    "green": "#15803D", "orange": "#EA580C",
}

# (code, internal_name, display_name, abbrev, service_line, order,
#  allows_new, allows_established, new_dur, est_dur, base_color)
CATALOG = [
    ("COMP_VISION", "Comprehensive Vision Exam", "Comprehensive Vision Exam", "COMP VIS", "Vision", 10,
     True, True, 20, 20, None),
    ("MED_EYE_EVAL", "Medical Eye Evaluation", "Medical Eye Evaluation", "MED EYE", "Medical Eye Care", 20,
     True, True, 20, 20, SEED_COLOR["teal"]),
    ("DRY_EYE_CONSULT", "Dry Eye Consultation", "Dry Eye Consultation", "DE CONS", "Dry Eye", 30,
     True, True, 30, 20, None),  # color administrator-assigned; inactive until color is set (spec 9.2)
    ("DRY_EYE_FOLLOWUP", "Dry Eye Follow-Up", "Dry Eye Follow-Up", "DE F/U", "Dry Eye", 40,
     True, True, 20, 20, None),
    ("CL_EVAL_CHECK", "Contact Lens Evaluation/Check", "Contact Lens Evaluation/Check", "CL EVAL", "Contact Lens", 50,
     True, True, 20, 20, SEED_COLOR["pink"]),
    ("POST_OP", "Post-Op Exam", "Post-Op Exam", "POST-OP", "Medical Eye Care", 60,
     False, True, None, 20, SEED_COLOR["green"]),
    ("VISITING_PHYSICIAN", "Visiting Physician", "Visiting Physician", "VISIT MD", "Medical Eye Care", 70,
     False, True, None, 15, SEED_COLOR["orange"]),
]

def migration_002_seed_appointment_types(conn):
    """Seed the 7 initial appointment types + color rules (spec 8.1 / 9.2 / 9.3)."""
    if not _table_exists(conn, "appointment_types"):
        return  # create_all() has not run yet in this ordering; caller re-invokes after create_all.
    existing = {r[0] for r in conn.execute(text("SELECT code FROM appointment_types")).fetchall()}
    now = datetime.utcnow().isoformat()
    for code, internal, display, abbr, line, order, allow_new, allow_est, new_dur, est_dur, base_color in CATALOG:
        if code in existing:
            continue
        # Dry Eye types stay inactive until an admin assigns a color (spec 9.2); COMP_VISION and
        # MED_EYE_EVAL are colored entirely by conditional rules (added below) rather than base_color.
        active = base_color is not None or code in ("COMP_VISION", "MED_EYE_EVAL")
        # is_system_seeded=0 here: these are ordinary, admin-editable catalog types that merely
        # ship pre-populated (spec 8.1). Only LEGACY_UNCLASSIFIED (migration 004) is a true
        # system-only, non-bookable type.
        # Boolean columns must bind as Python bool / SQL TRUE-FALSE, not 0/1 --
        # Postgres rejects an implicit int->boolean cast that SQLite allows.
        type_id = conn.execute(text("""
            INSERT INTO appointment_types (code, created_at, created_by_user_id, is_system_seeded, active)
            VALUES (:code, :now, NULL, FALSE, TRUE)
            RETURNING id
        """), {"code": code, "now": now}).scalar_one()
        version_id = conn.execute(text("""
            INSERT INTO appointment_type_versions
            (appointment_type_id, version_number, internal_name, display_name, calendar_abbreviation,
             description, service_line, display_order, allows_new, allows_established,
             new_duration_minutes, established_duration_minutes, buffer_before_minutes, buffer_after_minutes,
             arrival_lead_minutes, base_color, staff_bookable, patient_bookable, effective_from,
             effective_through, active, change_reason, created_at, created_by_user_id)
            VALUES (:tid, 1, :internal, :display, :abbr, :descr, :line, :order, :allow_new, :allow_est,
             :new_dur, :est_dur, 0, 0, 0, :base_color, TRUE, FALSE, :eff, NULL, :active, 'Initial seed', :now, NULL)
            RETURNING id
        """), {
            "tid": type_id, "internal": internal, "display": display, "abbr": abbr,
            "descr": f"System-seeded {display} appointment type.", "line": line, "order": order,
            "allow_new": bool(allow_new), "allow_est": bool(allow_est), "new_dur": new_dur, "est_dur": est_dur,
            "base_color": base_color, "eff": now[:10], "active": bool(active), "now": now,
        }).scalar_one()
        if code == "MED_EYE_EVAL":
            # Medical color precedence, exactly as specified in section 9.3.
            rules = [
                (1, "new", None, None, None, SEED_COLOR["red"], "new_patient"),
                (2, "established", 1, None, None, SEED_COLOR["teal"], "established_follow_up"),
                (3, "established", 0, 0, 2, SEED_COLOR["teal"], "established_0_to_2_tests"),
                (4, "established", 0, 3, None, SEED_COLOR["dark_blue"], "established_3_plus_tests"),
            ]
            for pri, rel, fu, mn, mx, color, reason in rules:
                # fu is 0/1/None in the table above (None = "don't care"); is_follow_up
                # is a real boolean column, so normalize before binding.
                conn.execute(text("""
                    INSERT INTO appointment_type_color_rules
                    (appointment_type_version_id, priority, patient_relationship, is_follow_up,
                     minimum_countable_tests, maximum_countable_tests, color, reason_code)
                    VALUES (:vid, :pri, :rel, :fu, :mn, :mx, :color, :reason)
                """), {"vid": version_id, "pri": pri, "rel": rel, "fu": None if fu is None else bool(fu),
                          "mn": mn, "mx": mx, "color": color, "reason": reason})
        elif code == "COMP_VISION":
            for pri, rel, color, reason in [
                (1, "new", SEED_COLOR["dark_purple"], "new_patient"),
                (2, "established", SEED_COLOR["light_purple"], "established_patient"),
            ]:
                conn.execute(text("""
                    INSERT INTO appointment_type_color_rules
                    (appointment_type_version_id, priority, patient_relationship, is_follow_up,
                     minimum_countable_tests, maximum_countable_tests, color, reason_code)
                    VALUES (:vid, :pri, :rel, NULL, NULL, NULL, :color, :reason)
                """), {"vid": version_id, "pri": pri, "rel": rel, "color": color, "reason": reason})

def migration_003_seed_diagnostic_tests(conn):
    if not _table_exists(conn, "diagnostic_tests"):
        return
    existing = {r[0] for r in conn.execute(text("SELECT code FROM diagnostic_tests")).fetchall()}
    tests = [
        ("OCT", "Optical Coherence Tomography", "OCT", 1, True, 10, 10),
        ("OPTOS", "Optos Widefield Retinal Imaging", "OPTOS", 1, True, 10, 20),
        ("VF", "Virtual Visual Field", "VF", 1, True, 10, 30),
        ("CORNEAL_ANALYZER", "Corneal Analyzer / Topography", "TOPO", 1, True, 10, 40),
        ("ERG", "Electroretinogram", "ERG", 1, True, 20, 50),
        ("MEIBOGRAPHY", "Meibography", "MEIBO", 1, True, 10, 60),
        ("TEARLAB", "TearLab Osmolarity", "TEARLAB", 1, True, 5, 70),
    ]
    for code, name, abbr, active, counts, dur, order in tests:
        if code in existing:
            continue
        conn.execute(text("""
            INSERT INTO diagnostic_tests (code, display_name, calendar_abbreviation, active,
                counts_toward_color, default_duration_minutes, display_order)
            VALUES (:code, :name, :abbr, :active, :counts, :dur, :order)
        """), {"code": code, "name": name, "abbr": abbr, "active": bool(active), "counts": bool(counts),
                  "dur": dur, "order": order})

def migration_004_legacy_appointment_type(conn):
    """Create the LEGACY_UNCLASSIFIED system type (spec 22.2) if missing."""
    if not _table_exists(conn, "appointment_types"):
        return
    row = conn.execute(text("SELECT id FROM appointment_types WHERE code = 'LEGACY_UNCLASSIFIED'")).fetchone()
    if row:
        return
    now = datetime.utcnow().isoformat()
    type_id = conn.execute(text("""
        INSERT INTO appointment_types (code, created_at, created_by_user_id, is_system_seeded, active)
        VALUES ('LEGACY_UNCLASSIFIED', :now, NULL, TRUE, FALSE)
        RETURNING id
    """), {"now": now}).scalar_one()
    conn.execute(text("""
        INSERT INTO appointment_type_versions
        (appointment_type_id, version_number, internal_name, display_name, calendar_abbreviation,
         description, service_line, display_order, allows_new, allows_established,
         new_duration_minutes, established_duration_minutes, buffer_before_minutes, buffer_after_minutes,
         arrival_lead_minutes, base_color, staff_bookable, patient_bookable, effective_from,
         effective_through, active, change_reason, created_at, created_by_user_id)
        VALUES (:tid, 1, 'Legacy/Unclassified Appointment', 'Legacy/Unclassified Appointment', 'LEGACY',
         'System type preserving pre-migration appointment records. Not bookable.', 'Legacy', 9999, TRUE, TRUE,
         NULL, NULL, 0, 0, 0, '#94A3B8', FALSE, FALSE, :eff, NULL, TRUE, 'Migration 22.2', :now, NULL)
    """), {"tid": type_id, "eff": now[:10], "now": now})

def migration_005_backfill_legacy_appointments(conn):
    """Link pre-existing appointments to LEGACY_UNCLASSIFIED and set a best-guess
    new/established snapshot per spec 22.3. Only touches rows not yet classified,
    so it is safe to re-run."""
    if not _table_exists(conn, "appointments") or not _table_exists(conn, "appointment_types"):
        return
    row = conn.execute(text("""
        SELECT v.id FROM appointment_type_versions v
        JOIN appointment_types t ON t.id = v.appointment_type_id
        WHERE t.code = 'LEGACY_UNCLASSIFIED'
    """)).fetchone()
    if not row:
        return
    legacy_version_id = row[0]
    targets = conn.execute(text("""
        SELECT id, patient_id, scheduled_at FROM appointments WHERE appointment_type_version_id IS NULL
    """)).fetchall()
    has_eye_exams = _table_exists(conn, "eye_exams")
    for appt_id, patient_id, scheduled_at in targets:
        relationship = "established"  # default per scoping note; overridden below when EyeExam history exists
        if has_eye_exams:
            exam_row = conn.execute(text("""
                SELECT 1 FROM eye_exams WHERE patient_id = :pid AND exam_date < :sched LIMIT 1
            """), {"pid": patient_id, "sched": str(scheduled_at)[:10]}).fetchone()
            relationship = "established" if exam_row else "new"
        # TODO(spec 22.3): when a real user/admin review workflow exists, route ambiguous
        # legacy appointments (no eye_exams table, or exam dates equal to appt date) through
        # a migration review report for administrative confirmation before treating this
        # inferred value as final. For now it is applied automatically and is audit-logged.
        conn.execute(text("""
            UPDATE appointments
            SET appointment_type_version_id = :vid,
                patient_relationship_at_booking = :rel,
                patient_relationship_source = 'migration_inferred',
                buffer_before_minutes = COALESCE(buffer_before_minutes, 0),
                buffer_after_minutes = COALESCE(buffer_after_minutes, 0),
                arrival_lead_minutes = COALESCE(arrival_lead_minutes, 0),
                resolved_color = COALESCE(resolved_color, '#94A3B8'),
                resolved_color_reason = COALESCE(resolved_color_reason, 'legacy_unclassified'),
                updated_at = :now
            WHERE id = :aid
        """), {"vid": legacy_version_id, "rel": relationship, "now": datetime.utcnow().isoformat(), "aid": appt_id})
        if _table_exists(conn, "appointment_audit_events"):
            conn.execute(text("""
                INSERT INTO appointment_audit_events
                (appointment_id, event_type, field_name, old_value, new_value, reason, actor_user_id, occurred_at)
                VALUES (:aid, 'migration_classified', 'patient_relationship_at_booking', NULL, :rel,
                        'Legacy appointment migration (spec 22.3): inferred from EyeExam history', NULL, :now)
            """), {"aid": appt_id, "rel": relationship, "now": datetime.utcnow().isoformat()})

# ---------------------------------------------------------------------------
# Migration: 006 -- create `practice_closures` table (Holidays/Closures screen)
# Migration: 007 -- create `daily_closings` table (Store Ops > Daily Closing)
# Brand-new tables, so plain CREATE TABLE IF NOT EXISTS is safe/idempotent even
# though create_all() would also create these on a fresh database -- per the
# migration-runner convention in this file, schema additions for existing
# databases must not depend on create_all() alone.
# ---------------------------------------------------------------------------
def migration_006_create_practice_closures(conn):
    conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS practice_closures (
            id {_pk_ddl(conn)},
            closure_date VARCHAR NOT NULL UNIQUE,
            label VARCHAR NOT NULL,
            notes TEXT,
            created_at TIMESTAMP
        )
    """))

def migration_007_create_daily_closings(conn):
    conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS daily_closings (
            id {_pk_ddl(conn)},
            posting_date VARCHAR NOT NULL,
            payment_type VARCHAR NOT NULL,
            calculated_amount FLOAT DEFAULT 0.0,
            actual_amount FLOAT DEFAULT 0.0,
            variance FLOAT DEFAULT 0.0,
            explanation TEXT,
            created_at TIMESTAMP
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_daily_closings_posting_date ON daily_closings (posting_date)"))

# ---------------------------------------------------------------------------
# Migration: 008 -- add `patients.balance_due` (manually-entered balance snapshot
# driving the three-state patient-context-strip balance box: due/even/credit).
# ---------------------------------------------------------------------------
def migration_008_patient_balance_due(conn):
    if not _table_exists(conn, "patients"):
        return  # brand-new database; create_all() will create the full table with this column.
    _add_column_if_missing(conn, "patients", "balance_due", "FLOAT")

# ---------------------------------------------------------------------------
# Migration: 009 -- add `patients.preferred_name` and `patients.mrn`.
# Both nullable, no UNIQUE constraint on mrn (see Patient.mrn comment in
# ehr/models/database.py for why uniqueness enforcement is deferred).
# ---------------------------------------------------------------------------
def migration_009_patient_preferred_name_mrn(conn):
    if not _table_exists(conn, "patients"):
        return  # brand-new database; create_all() will create the full table with these columns.
    _add_column_if_missing(conn, "patients", "preferred_name", "VARCHAR")
    _add_column_if_missing(conn, "patients", "mrn", "VARCHAR")

# ---------------------------------------------------------------------------
# Migration: 010 -- enforce `patients.mrn` uniqueness.
# Two steps, both required since real data may already have collisions:
#   1. Data cleanup: blank strings are normalized to NULL (NULL is exempt from
#      the uniqueness check, matching "no MRN assigned yet"); for any MRN value
#      shared by more than one patient, only the lowest-id patient keeps it --
#      every later duplicate is cleared to NULL rather than silently kept
#      wrong, and is audit-logged into appointment_audit_events-style history
#      is not available for patients, so instead each cleared row is recorded
#      into a lightweight `mrn_deduplication_log` table for administrative
#      follow-up (which patient, which MRN value, when).
#   2. A partial UNIQUE index (`WHERE mrn IS NOT NULL`) is created so SQLite
#      itself now rejects a future duplicate, and the application layer
#      (create_patient/update_patient) checks for a conflict first and returns
#      a friendly 400 instead of ever letting that constraint raise a raw
#      IntegrityError.
# ---------------------------------------------------------------------------
def migration_010_patient_mrn_uniqueness(conn):
    if not _table_exists(conn, "patients"):
        return  # brand-new database; create_all() creates the unique index directly.
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS mrn_deduplication_log ("
        f"id {_pk_ddl(conn)}, patient_id INTEGER NOT NULL, "
        "cleared_mrn VARCHAR NOT NULL, occurred_at TEXT NOT NULL)"
    ))
    # Normalize blank/whitespace-only MRNs to NULL so they don't collide with each other.
    conn.execute(text("UPDATE patients SET mrn = NULL WHERE mrn IS NOT NULL AND trim(mrn) = ''"))
    now = datetime.utcnow().isoformat()
    dupes = conn.execute(text(
        "SELECT mrn FROM patients WHERE mrn IS NOT NULL GROUP BY mrn HAVING COUNT(*) > 1"
    )).fetchall()
    for (mrn_value,) in dupes:
        rows = conn.execute(text(
            "SELECT id FROM patients WHERE mrn = :mrn ORDER BY id ASC"
        ), {"mrn": mrn_value}).fetchall()
        for (patient_id,) in rows[1:]:  # keep the first (lowest id), clear the rest
            conn.execute(text("UPDATE patients SET mrn = NULL WHERE id = :pid"), {"pid": patient_id})
            conn.execute(text(
                "INSERT INTO mrn_deduplication_log (patient_id, cleared_mrn, occurred_at) "
                "VALUES (:pid, :mrn, :now)"
            ), {"pid": patient_id, "mrn": mrn_value, "now": now})
    conn.execute(text(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_patients_mrn_unique ON patients (mrn) WHERE mrn IS NOT NULL"
    ))

# ---------------------------------------------------------------------------
# Migration: 011 -- seed Resource rows and AppointmentTypeResourceRequirement
# links so real-resource conflict detection (ehr/services/scheduling.py) has
# real data to check against (spec 26.10 item 1's documented gap).
#
# Seed choices (kept here as the single source of truth for why these were
# picked, rather than scattered comments):
#   - Lane 1 / Lane 2 (resource_class='exam_lane'): general exam lanes.
#   - Contact Lens Fitting Room (resource_class='room'): required by
#     CL_EVAL_CHECK (Contact Lens Evaluation/Check) -- a contact lens fitting
#     visit clinically needs the fitting room and its equipment every time.
#   - OCT Machine (resource_class='device'): seeded so a device-class resource
#     exists, conceptually tied to the OCT diagnostic test -- but deliberately
#     NOT wired to any AppointmentTypeResourceRequirement here. OCT is an
#     optional per-visit diagnostic test (AppointmentTest already has its own
#     required_resource_id column for that finer-grained, per-test case), not
#     something every appointment of some type always needs.
#   - COMP_VISION (Comprehensive Vision Exam) requires an exam lane (Lane 1)
#     since a full exam always needs a lane to be performed in.
#   - Every other seeded appointment type is intentionally left resource-free,
#     matching real-world variability (not every visit type needs a resource).
# ---------------------------------------------------------------------------
def migration_011_seed_resources_and_requirements(conn):
    if not _table_exists(conn, "resources") or not _table_exists(conn, "appointment_type_resource_requirements"):
        return  # brand-new database ordering edge case; POST_CREATE_ALL_MIGRATIONS always runs after create_all().
    existing_codes = {r[0] for r in conn.execute(text("SELECT code FROM resources")).fetchall()}
    resource_rows = [
        ("LANE1", "Lane 1", "exam_lane"),
        ("LANE2", "Lane 2", "exam_lane"),
        ("CL_ROOM", "Contact Lens Fitting Room", "room"),
        ("OCT_DEVICE", "OCT Machine", "device"),
    ]
    ids = {}
    for code, name, cls in resource_rows:
        if code in existing_codes:
            row = conn.execute(text("SELECT id FROM resources WHERE code = :c"), {"c": code}).fetchone()
            ids[code] = row[0]
            continue
        ids[code] = conn.execute(text("""
            INSERT INTO resources (code, display_name, resource_class, exclusive, active)
            VALUES (:code, :name, :cls, TRUE, TRUE)
            RETURNING id
        """), {"code": code, "name": name, "cls": cls}).scalar_one()

    def _current_type_version_id(code):
        row = conn.execute(text("""
            SELECT v.id FROM appointment_type_versions v
            JOIN appointment_types t ON t.id = v.appointment_type_id
            WHERE t.code = :code ORDER BY v.version_number DESC LIMIT 1
        """), {"code": code}).fetchone()
        return row[0] if row else None

    requirement_specs = [
        ("COMP_VISION", "LANE1"),
        ("CL_EVAL_CHECK", "CL_ROOM"),
    ]
    for type_code, resource_code in requirement_specs:
        version_id = _current_type_version_id(type_code)
        resource_id = ids.get(resource_code)
        if not version_id or not resource_id:
            continue
        already = conn.execute(text("""
            SELECT 1 FROM appointment_type_resource_requirements
            WHERE appointment_type_version_id = :vid AND resource_id = :rid
        """), {"vid": version_id, "rid": resource_id}).fetchone()
        if already:
            continue
        conn.execute(text("""
            INSERT INTO appointment_type_resource_requirements
            (appointment_type_version_id, resource_id, resource_pool_code, required, offset_minutes, duration_minutes)
            VALUES (:vid, :rid, NULL, TRUE, 0, NULL)
        """), {"vid": version_id, "rid": resource_id})

# ---------------------------------------------------------------------------
# Migration: 014 -- create `provider_availability_templates`/
# `provider_availability_exceptions` tables. Brand-new tables, plain CREATE
# TABLE IF NOT EXISTS (same convention as 006/007) -- these are the
# provider-scoped counterpart to the resource-scoped AvailabilityTemplate/
# AvailabilityException tables, and power the real open-slot availability
# search (ehr/services/scheduling.py's find_open_slots). See
# ProviderAvailabilityTemplate's docstring in ehr/models/database.py for why
# this is a separate table rather than a nullable provider_id added to the
# existing resource-scoped tables.
# ---------------------------------------------------------------------------
def migration_014_create_provider_availability_tables(conn):
    conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS provider_availability_templates (
            id {_pk_ddl(conn)},
            provider_id INTEGER NOT NULL,
            day_of_week INTEGER NOT NULL,
            start_time VARCHAR NOT NULL,
            end_time VARCHAR NOT NULL,
            effective_from VARCHAR,
            effective_through VARCHAR,
            active BOOLEAN DEFAULT TRUE
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_provider_availability_templates_provider_day "
        "ON provider_availability_templates (provider_id, day_of_week)"
    ))
    conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS provider_availability_exceptions (
            id {_pk_ddl(conn)},
            provider_id INTEGER NOT NULL,
            start_at TIMESTAMP NOT NULL,
            end_at TIMESTAMP NOT NULL,
            exception_type VARCHAR DEFAULT 'blocked',
            reason VARCHAR
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_provider_availability_exceptions_provider_window "
        "ON provider_availability_exceptions (provider_id, start_at, end_at)"
    ))

# ---------------------------------------------------------------------------
# Migration: 012 -- create `users`, `user_sessions`, `auth_audit_events` tables
# (real-authentication pass). Brand-new tables -- plain CREATE TABLE IF NOT
# EXISTS is safe/idempotent even though create_all() would also create these
# on a fresh database, per this file's convention that schema additions for
# EXISTING databases must not depend on create_all() alone.
# ---------------------------------------------------------------------------
def migration_012_create_auth_tables(conn):
    conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS users (
            id {_pk_ddl(conn)},
            email VARCHAR NOT NULL UNIQUE,
            password_hash VARCHAR NOT NULL,
            password_salt VARCHAR NOT NULL,
            first_name VARCHAR NOT NULL,
            last_name VARCHAR NOT NULL,
            role VARCHAR NOT NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP,
            last_login_at TIMESTAMP
        )
    """))
    conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users (email)"))
    conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS user_sessions (
            id {_pk_ddl(conn)},
            user_id INTEGER NOT NULL,
            session_token VARCHAR NOT NULL UNIQUE,
            created_at TIMESTAMP,
            last_seen_at TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            revoked BOOLEAN NOT NULL DEFAULT FALSE
        )
    """))
    conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_user_sessions_token ON user_sessions (session_token)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_user_sessions_user_revoked ON user_sessions (user_id, revoked)"))
    conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS auth_audit_events (
            id {_pk_ddl(conn)},
            event_type VARCHAR NOT NULL,
            user_id INTEGER,
            actor_email_attempted VARCHAR,
            ip_address VARCHAR,
            occurred_at TIMESTAMP,
            detail TEXT
        )
    """))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auth_audit_occurred_at ON auth_audit_events (occurred_at)"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auth_audit_user ON auth_audit_events (user_id)"))

# ---------------------------------------------------------------------------
# Migration: 013 -- add `appointments.created_by_user_id`/`updated_by_user_id`.
# Deferred since v1.3 (spec 18.2) for lack of a User model; that model exists
# now (migration 012, v2.4), so this closes that deferral. Plain nullable
# INTEGER columns, no physical FK enforcement attempted via ALTER TABLE (SQLite
# can't add one this way; the ORM-level ForeignKey is enough for a fresh
# create_all() database and for query-time joins either way).
# ---------------------------------------------------------------------------
def migration_013_appointment_created_updated_by(conn):
    if not _table_exists(conn, "appointments"):
        return  # brand-new database; create_all() will create the full table with these columns.
    _add_column_if_missing(conn, "appointments", "created_by_user_id", "INTEGER")
    _add_column_if_missing(conn, "appointments", "updated_by_user_id", "INTEGER")

# ---------------------------------------------------------------------------
# Migration: 015 -- Refractive Assessment & Plan structured fields
# (VISION_EHR_DATA_STANDARDS_RESEARCH.md 5.1), reviewed/reconciled in v2.9,
# built in v2.10. Structured Assessment columns on eye_exams, structured Plan
# (lens design/follow-up) columns on prescriptions. Plain nullable VARCHAR --
# no DB-level enum, matching every other multi-choice field in this schema.
# ---------------------------------------------------------------------------
def migration_015_refractive_assessment_and_plan(conn):
    if _table_exists(conn, "eye_exams"):
        _add_column_if_missing(conn, "eye_exams", "refractive_diagnosis", "VARCHAR")
        _add_column_if_missing(conn, "eye_exams", "refractive_laterality", "VARCHAR")
        _add_column_if_missing(conn, "eye_exams", "refractive_stability", "VARCHAR")
        _add_column_if_missing(conn, "eye_exams", "refractive_secondary_findings", "VARCHAR")
    if _table_exists(conn, "prescriptions"):
        _add_column_if_missing(conn, "prescriptions", "lens_type", "VARCHAR")
        _add_column_if_missing(conn, "prescriptions", "lens_material", "VARCHAR")
        _add_column_if_missing(conn, "prescriptions", "lens_treatments", "VARCHAR")
        _add_column_if_missing(conn, "prescriptions", "recall_interval", "VARCHAR")
        _add_column_if_missing(conn, "prescriptions", "patient_education_tags", "VARCHAR")

# ---------------------------------------------------------------------------
# Migration: 016 -- create `anterior_segment_assessments`
# (VISION_EHR_DATA_STANDARDS_RESEARCH.md 5.2), reviewed in v2.9, built in
# v2.11. Brand-new table -- plain CREATE TABLE IF NOT EXISTS, same pattern as
# migration 014.
# ---------------------------------------------------------------------------
def migration_016_create_anterior_segment_assessments(conn):
    conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS anterior_segment_assessments (
            id {_pk_ddl(conn)},
            exam_id INTEGER NOT NULL,
            primary_diagnosis_code VARCHAR,
            severity VARCHAR,
            conjunctival_injection_od VARCHAR, conjunctival_injection_os VARCHAR,
            corneal_staining_od VARCHAR, corneal_staining_os VARCHAR,
            mgd_expression_od VARCHAR, mgd_expression_os VARCHAR,
            tbut_seconds_od INTEGER, tbut_seconds_os INTEGER,
            schirmer_mm_od INTEGER, schirmer_mm_os INTEGER,
            plan_therapeutics VARCHAR,
            follow_up_interval VARCHAR,
            clinical_notes TEXT
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_anterior_segment_assessments_exam "
        "ON anterior_segment_assessments (exam_id)"
    ))

# ---------------------------------------------------------------------------
# Migration: 017 -- create `glaucoma_trackings`
# (VISION_EHR_DATA_STANDARDS_RESEARCH.md 5.3), reviewed in v2.9, built in
# v2.16. Brand-new table -- plain CREATE TABLE IF NOT EXISTS, same pattern as
# migrations 014/016.
# ---------------------------------------------------------------------------
def migration_017_create_glaucoma_trackings(conn):
    conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS glaucoma_trackings (
            id {_pk_ddl(conn)},
            exam_id INTEGER NOT NULL,
            primary_diagnosis_code VARCHAR,
            target_iop_od INTEGER, target_iop_os INTEGER,
            iop_current_od INTEGER, iop_current_os INTEGER,
            iop_time_measured VARCHAR,
            iop_method VARCHAR,
            cup_disc_ratio_od FLOAT, cup_disc_ratio_os FLOAT,
            nerve_tissue_status_od VARCHAR, nerve_tissue_status_os VARCHAR,
            oct_rnfl_average_microns_od INTEGER, oct_rnfl_average_microns_os INTEGER,
            visual_field_md_db_od FLOAT, visual_field_md_db_os FLOAT,
            vf_reliability_od VARCHAR, vf_reliability_os VARCHAR,
            prescribed_glaucoma_meds VARCHAR,
            diagnostic_orders VARCHAR,
            follow_up_interval VARCHAR,
            clinical_notes TEXT
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_glaucoma_trackings_exam "
        "ON glaucoma_trackings (exam_id)"
    ))

# ---------------------------------------------------------------------------
# Migration: 018 -- create `binocular_vision_assessments`
# (VISION_EHR_DATA_STANDARDS_RESEARCH.md 5.4), reviewed in v2.9, built in
# v2.17. Brand-new table -- plain CREATE TABLE IF NOT EXISTS, same pattern as
# migrations 016/017.
# ---------------------------------------------------------------------------
def migration_018_create_binocular_vision_assessments(conn):
    conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS binocular_vision_assessments (
            id {_pk_ddl(conn)},
            exam_id INTEGER NOT NULL,
            primary_diagnosis_code VARCHAR,
            phoria_distance_diopters INTEGER, phoria_near_diopters INTEGER,
            strabismus_present BOOLEAN,
            strabismus_direction VARCHAR,
            npc_break_cm FLOAT, npc_recovery_cm FLOAT,
            accommodation_amplitude_od FLOAT, accommodation_amplitude_os FLOAT,
            assigned_home_exercises VARCHAR,
            therapy_session_number INTEGER,
            therapy_compliance_rating VARCHAR,
            follow_up_interval VARCHAR,
            clinical_notes TEXT
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_binocular_vision_assessments_exam "
        "ON binocular_vision_assessments (exam_id)"
    ))

# ---------------------------------------------------------------------------
# Migration: 019 -- create `surgery_comanagement_trackings`
# (VISION_EHR_DATA_STANDARDS_RESEARCH.md 5.5), reviewed in v2.9, built in
# v2.18. Brand-new table -- plain CREATE TABLE IF NOT EXISTS, same pattern as
# migrations 017/018.
# ---------------------------------------------------------------------------
def migration_019_create_surgery_comanagement_trackings(conn):
    conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS surgery_comanagement_trackings (
            id {_pk_ddl(conn)},
            exam_id INTEGER NOT NULL,
            surgical_procedure VARCHAR,
            operative_eye VARCHAR,
            date_of_surgery VARCHAR,
            surgeon_name VARCHAR, co_managing_facility VARCHAR,
            current_milestone VARCHAR,
            best_corrected_visual_acuity VARCHAR,
            intraocular_pressure INTEGER,
            corneal_edema_present BOOLEAN,
            corneal_edema_grading VARCHAR,
            anterior_chamber_cells_flare VARCHAR,
            surgical_flap_or_wound_status VARCHAR,
            steroid_taper_schedule TEXT,
            nsaid_drops_frequency VARCHAR, antibiotic_drops_status VARCHAR,
            follow_up_interval VARCHAR,
            clinical_notes TEXT
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_surgery_comanagement_trackings_exam "
        "ON surgery_comanagement_trackings (exam_id)"
    ))

# Ordered list of (id, function). Adding new migrations: append, never edit past entries.
COLUMN_MIGRATIONS = [
    ("001_appointment_columns", migration_001_appointment_columns),
    ("006_create_practice_closures", migration_006_create_practice_closures),
    ("007_create_daily_closings", migration_007_create_daily_closings),
    ("008_patient_balance_due", migration_008_patient_balance_due),
    ("009_patient_preferred_name_mrn", migration_009_patient_preferred_name_mrn),
    ("010_patient_mrn_uniqueness", migration_010_patient_mrn_uniqueness),
    ("012_create_auth_tables", migration_012_create_auth_tables),
    ("013_appointment_created_updated_by", migration_013_appointment_created_updated_by),
    ("014_create_provider_availability_tables", migration_014_create_provider_availability_tables),
    ("015_refractive_assessment_and_plan", migration_015_refractive_assessment_and_plan),
    ("016_create_anterior_segment_assessments", migration_016_create_anterior_segment_assessments),
    ("017_create_glaucoma_trackings", migration_017_create_glaucoma_trackings),
    ("018_create_binocular_vision_assessments", migration_018_create_binocular_vision_assessments),
    ("019_create_surgery_comanagement_trackings", migration_019_create_surgery_comanagement_trackings),
]
POST_CREATE_ALL_MIGRATIONS = [
    ("002_seed_appointment_types", migration_002_seed_appointment_types),
    ("003_seed_diagnostic_tests", migration_003_seed_diagnostic_tests),
    ("004_legacy_appointment_type", migration_004_legacy_appointment_type),
    ("005_backfill_legacy_appointments", migration_005_backfill_legacy_appointments),
    ("011_seed_resources_and_requirements", migration_011_seed_resources_and_requirements),
]

def run_column_migrations(engine):
    """Phase 1: ALTER TABLE migrations on pre-existing tables. Must run BEFORE create_all()."""
    with engine.begin() as conn:
        if not _is_postgres(conn):
            conn.execute(text("PRAGMA foreign_keys=ON"))  # Postgres enforces FKs unconditionally.
        _ensure_migrations_table(conn)
        for migration_id, func in COLUMN_MIGRATIONS:
            if not _applied(conn, migration_id):
                func(conn)
                _mark_applied(conn, migration_id)

def run_post_create_all_migrations(engine):
    """Phase 2: data-seeding migrations that require the new tables to already exist.
    Must run AFTER Base.metadata.create_all()."""
    with engine.begin() as conn:
        _ensure_migrations_table(conn)
        for migration_id, func in POST_CREATE_ALL_MIGRATIONS:
            if not _applied(conn, migration_id):
                func(conn)
                _mark_applied(conn, migration_id)

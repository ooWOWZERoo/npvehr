# New Path Vision EHR

Local Python/FastAPI optometry practice EHR prototype.

**Start here:** `NEW_PATH_VISION_EHR_BASELINE_PRODUCT_DEFINITION_AND_SPECIFICATION.md` is the living
specification — the authoritative record of what's implemented, what's placeholder, and what's
explicitly deferred. Read the go-live safety notice at the top of that document before doing
anything with real patient data: authentication now exists (v2.4), but compliant hosting,
encryption, and backups do not yet, and none of the three real-infrastructure decisions in this
repo's next phase (Neon/Vercel/Cloudinary) satisfy HIPAA requirements on their own or without a
signed BAA at an appropriate plan tier — confirm with compliance counsel before any real PHI.

`VISION_EHR_DATA_STANDARDS_RESEARCH.md` is reference material on eye-care-specific EHR data
standards (FHIR, IHE, DICOM, SNOMED/LOINC/ICD-10) for a future clinical-data-model rework — not
yet implemented.

## Running locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn ehr.app:app --host 0.0.0.0 --port 8000 --reload
```

The app expects to be run from this directory (paths to the database, templates, and static
assets are relative to the working directory). On first run against an empty database (local
SQLite by default, or a fresh Postgres database via `DATABASE_URL`) it auto-seeds demo data and
four demo login accounts — printed to the console the first time. This is idempotent and runs on
every startup, so it's a no-op once a database already has accounts in it.

## Testing

End-to-end tests (Playwright, driven via pytest) launch the real app against a
throwaway, freshly-seeded SQLite database and exercise it in a real browser --
login, auth redirects, and the main navigation destinations.

```bash
pip install -r requirements.txt -r requirements-dev.txt
playwright install chromium   # first time only, downloads the browser
pytest
```

## Structure

- `ehr/app.py` — FastAPI app entrypoint, router registration, startup migrations
- `ehr/models/database.py` — SQLAlchemy models (patients, appointments, exams, prescriptions,
  the appointment scheduling module, auth/roles/audit, practice operations, etc.)
- `ehr/db/migrations.py` — idempotent migration runner (schema_migrations-tracked)
- `ehr/db/seed.py` — demo data + demo account seeding
- `ehr/routes/` — patients, appointments, exams, prescriptions, admin scheduling, store
  operations/orders/claims/catalog
- `ehr/auth/` — authentication, session management, role permissions, audit logging
- `ehr/templates/` — Jinja2 templates
- `ehr/static/` — CSS, JS, logo, patient photo uploads


# New Path Vision EHR — Working Notes for Claude Code

Read this before starting work in this repo. It captures conventions that
have been re-derived repeatedly across sessions; following them avoids
rebuilding things that already exist or breaking established patterns.

## Orientation

- **Source of truth for what's already built:** `NEW_PATH_VISION_EHR_BASELINE_PRODUCT_DEFINITION_AND_SPECIFICATION.md`
  (versioned, one numbered section + change-log table per shipped feature).
  Read the most recent sections before proposing new work — the feature
  you're about to build may already exist.
- **Source of truth for what's left:** `BUILD_BACKLOG.md`. Update both docs
  as part of any feature, not as an afterthought.
- **Explicitly not for real patient data** — see the warning banner at the
  top of the spec doc. Nothing in this app transmits/submits real
  billing/claims data; billing features are staff-facing decision support
  only (advisory banners, read-only previews), never an outbound
  integration, unless a user explicitly asks to cross that line.

## Established conventions (do these, don't reinvent)

- **Narrow curated lookup table, not a live rules engine** — the posture for
  ICD-10 (`ap_composer.py`), CPT (`cpt_mapper.py`), and NCCI PTP edits
  (`ncci_edits.py`). When asked to add "compliance" logic, default to a
  small hardcoded table sourced from a primary document, not a general
  rules service.
- **Ambient banner, never a blocking modal** — look-back alerts, follow-up
  recommendations, NCCI advisories all render as `.alert-info`/
  `.alert-warning` banners with inline action buttons, never a popup.
- **Override pattern**: a checkbox + a required reason field, e.g.
  `conflict_override`/`conflict_override_reason`,
  `duration_override`/`duration_override_reason`,
  `billing_override`/`billing_override_reason`. Reuse this shape for any
  new override rather than inventing a new one.
- **Migrations**: `ehr/db/migrations.py`, two ordered lists
  (`COLUMN_MIGRATIONS`, `POST_CREATE_ALL_MIGRATIONS`), each entry
  `(id, func)`. A **brand-new table** needs no raw `CREATE TABLE` if it's a
  plain SQLAlchemy model — `Base.metadata.create_all()` makes it for free.
  Migrations are for **adding columns to existing tables**
  (`_add_column_if_missing`) and **data backfills**. Every migration must be
  idempotent (safe to re-run) — guard with `_table_exists`/`_column_exists`
  and a re-run-safety check before inserting backfill rows.
- **Audit trail for a new business event**: a small dedicated table (see
  `AppointmentAuditEvent`, `FieldChangeAuditEvent`, `BillingOverrideEvent`),
  not a generic catch-all. Real accountability data (who, when, what,
  why) — not silently dropped.
- **Role permissions**: centralized in `ehr/auth/permissions.py` as named
  set constants (`EXAM_EDIT`, `BILLING_OVERRIDE`, etc.), consumed via
  `Depends(require_role(*CONSTANT))`. System Administrator is the
  override-everything role and belongs in most permission sets for that
  reason (matches `EXAM_SIGN`, `USER_MANAGEMENT`, `BILLING_OVERRIDE`).
- **Diagnostic orders**: `DiagnosticOrder` (patient-scoped, persists across
  visits, real `ordered → scheduled → in_progress → completed/cancelled`
  lifecycle via `ehr.services.diagnostic_orders.transition()`) is distinct
  from `AppointmentTest` (calendar-slot scheduling/duration credit only).
  The two intentionally coexist — don't retrofit one into the other.

## Testing

- **Playwright** (`tests/test_smoke.py`) drives the real app end-to-end —
  no browser mocking. `tests/test_migrations.py` is unit-level, no browser,
  a throwaway SQLite file per test.
- **Sandbox chromium override**: this environment's Playwright can't reach
  the normal browser download, so add this fixture to `tests/conftest.py`
  before running the suite, and **always remove it again before
  committing** (`git diff tests/conftest.py` must be empty on commit):
  ```python
  @pytest.fixture(scope="session")
  def browser_type_launch_args(browser_type_launch_args):
      return {**browser_type_launch_args, "executable_path": "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"}
  ```
- Run the **full suite** before committing, not just the tests you touched
  — this app has cross-feature interactions (e.g. NCCI checks appear on
  both the exam form and the booking form).
- A pre-existing, unrelated failure should be confirmed by stashing your
  changes and re-running against the base commit before you write it off —
  don't assume.

## Known pre-existing issue (as of v2.61 / PR #34)

`test_portal_phase4_followups` fails intermittently-but-reproducibly on a
`Locator.get_attribute` timeout waiting for `a[href*="/reschedule"]` on
`/portal/appointments?booked=1`. Confirmed to reproduce on the unmodified
base commit (unrelated to portal/reschedule changes) — not yet root-caused.
Worth investigating as its own task; don't assume a future red run of this
specific test is caused by your change without checking.

## Workflow

- One PR per logical change. Full suite green (or a confirmed-unrelated
  failure, documented) before committing.
- Commit messages end with the `Co-Authored-By`/`Claude-Session` footer;
  PR bodies end with the `Generated with Claude Code` + session link
  footer — see the system reminder in-session for exact current text
  (it can change per session).
- Ask before merging a standalone (non-batch) PR unless the user has
  already said "watch and merge when green" for that PR.
- Update `BUILD_BACKLOG.md` and the spec doc's next numbered section
  (with its own change-log table) as part of the same PR, not a follow-up.

# New Path Vision EHR — Master Build Backlog

**Status:** Living tracking document. **Baseline as of:** spec v2.19 / research doc v2.18 (2026-09-11).

## Purpose and how to use this document

This consolidates every outstanding build item, investigation, and test scattered across `NEW_PATH_VISION_EHR_BASELINE_PRODUCT_DEFINITION_AND_SPECIFICATION.md` (the living spec), `VISION_EHR_DATA_STANDARDS_RESEARCH.md` (the eye-care data-standards research notes), and open GitHub issues, into one place — so a reader doesn't have to reconstruct current status from a dozen change-log entries across two documents.

**This document does not replace the spec or research doc as the source of truth for what's *already built*** — those two documents remain authoritative for current-state facts, and every item below cites exactly where its full detail lives. This document exists only to answer "what's left, and in what order," and to track status as items move.

**Conventions:**
- `[ ]` not started · `[~]` partially done / documented but not built · `[x]` done (kept here briefly for traceability, then prunable)
- Every item cites its source section(s) so detail isn't duplicated here.
- "Subtasks" follow the proven pattern established by every dashboard slice shipped so far (§5.1/§5.2 in the research doc): schema → migration (dialect-verified on SQLite + Postgres) → routes → templates → seed data → Playwright coverage → spec update. That exact 7-step shape is reused as the subtask list for every net-new feature below rather than re-deriving it each time.
- When picking up an item, move it under "In Progress," and when it ships, update its status here **and** in the spec/research doc via the normal versioned-changelog process — this document itself gets no version number of its own; it just tracks live status.

---

## 0. In Progress / Up Next

- [x] **ICD-10 auto-suggestion + diagnosis-driven recall interval** — extends the v2.12 Assessment & Plan composer. **Done, v2.15** — see baseline spec §12.5d, research doc §7.2.
- [x] **Posterior Segment & Glaucoma Tracking dashboard, with trend view** — third of five clinical dashboards. **Done, v2.16** — see baseline spec §12.5e, research doc §5.3.
- [x] **Binocular Vision & Pediatrics (Vision Therapy) dashboard** — fourth of five clinical dashboards. **Done, v2.17** — see baseline spec §12.5f, research doc §5.4.
- [x] **Pre-/Post-Operative Co-Management dashboard, with timeline view** — fifth and last clinical dashboard. **Done, v2.18** — see baseline spec §12.5g, research doc §5.5. §1 (Clinical Dashboards) is now fully closed out.
- [x] **CSRF protection** — a long-tracked security gap (§7). **Done, v2.19** — see baseline spec §37.7. Pick the next item from the sections below.

---

## 1. Clinical Dashboards (research doc §5) — closed out, v2.18

All five dashboards from the original reviewed requirements document are now built (v2.10–v2.18), each following the same 7-step pattern — a child table keyed on `exam_id`, one Visit Focus chip, one wrapped `<div>` in `exams/form.html`, no changes to the toggle/composer mechanism itself. Kept here briefly for traceability.

- [x] **§5.3 Posterior Segment & Glaucoma Tracking dashboard. Done, v2.16** — see baseline spec §12.5e. Built in full, including the longitudinal IOP trend view (a new patient-workspace tab with a server-computed inline SVG chart) — confirmed with the user to build the complete spec rather than deferring trending as a separate follow-up.
  - [x] Schema: new `GlaucomaTracking` table (exam-scoped child row)
  - [x] Migration (dialect-verified: `017_create_glaucoma_trackings`)
  - [x] Routes: `create_exam` conditional row creation; new `GET /patients/{id}/glaucoma-trend`
  - [x] Templates: new Visit Focus chip + section in `exams/form.html`, conditional card in `exams/detail.html`, new `patients/glaucoma_trend_tab.html`
  - [x] Trend view built (resolved, not deferred)
  - [x] Seed data (two glaucoma-tracking exams, 6 months apart, same demo patient)
  - [x] Playwright coverage (`test_glaucoma_focus_toggle_composer_and_trend_view`)
  - [x] Spec update
- [x] **§5.4 Binocular Vision & Pediatrics (Vision Therapy) dashboard. Done, v2.17** — see baseline spec §12.5f. No trend view needed (nothing in this field list asks for cross-visit trending beyond a plain session-number counter).
  - [x] Schema, migration, routes, templates, seed, tests, spec (same 7-step pattern)
- [x] **§5.5 Pre-/Post-Operative Co-Management dashboard. Done, v2.18** — see baseline spec §12.5g. The per-visit-row vs. single-row-with-mutable-milestone question resolved by reusing §5.3's Glaucoma trend-view precedent: every clinical encounter is already an `EyeExam` row, so "one row per follow-up visit" needs no new modeling primitive. All five clinical dashboards from research doc §5 are now built.
  - [x] Schema, migration, routes, templates, seed, tests, spec (same 7-step pattern)

---

## 2. Billing, Claims & Insurance (research doc §6) — target-state only

**Explicitly gated**: do not begin implementation without (a) a real clearinghouse/payer relationship, (b) compliance/legal review, and (c) the go-live BAA prerequisite (§9 below) resolved first — this domain has materially higher compliance and financial risk than any clinical dashboard, and this app remains marked "do not use with real patient data." Listed here for completeness/tracking, not as a queued build item.

- [ ] Billing invoice / service-line data model (CMS-1500-shaped fields — research doc §6.1)
- [ ] CCI-edit / medical-necessity rule-matrix lookup tables (§6.2) — the lookup-table-over-hardcoded-logic pattern itself is reusable even before/if the billing domain is greenlit
- [ ] Checkout-block workflow ("Pending Conflict" status + remediation surfacing, §6.3) — no checkout/payment flow exists in this app at all yet, so this depends on that existing first
- [ ] EDI 837 (X12) / CMS-1500 generation (§6.4) — flagged as a compliance-gated capability, not a formatting exercise; needs a real clearinghouse relationship before any code is written

---

## 3. E-prescribing, Optical Lab Integration & Inventory (research doc §5.6) — target-state only

Same external-integration caution as billing above, though lower compliance stakes (no direct payer/claims risk):

- [ ] E-prescribing (NCPDP SCRIPT, RxNorm drug identification, structured SIG codes)
- [ ] Optical lab order transmission (ANSI Z80/VisionWeb-style API, frame boxing/centration metrics, JSON order payload)
- [ ] In-house optical inventory (`inventory_frames`/`inventory_contact_lenses`, UPC/SKU lookup, reorder thresholds, transactional checkout with row-level locking)
- [ ] Real Order Management (turning a written Rx into a trackable lab order — placed → fabrication → shipped → received → dispensed). Recommended by Claude earlier this session as the natural "next step after the exam" in the patient journey (Optical/Billing Out per IHE GEE, research doc §4.2); not yet scoped or started. Existing placeholder describes the target shape already (`ehr/routes/store_ops.py`'s `/orders/`).

---

## 4. Interoperability & Coded Terminology (research doc §4, deferred since v2.8)

The foundational, largest-scope item underlying much of the above — deliberately deferred multiple times in favor of narrower slices (habitual/manifest/cycloplegic types in v2.8 was "item 3," the first slice):

- [ ] Decide whether/when to align `EyeExam`/`Refraction`/`Prescription` toward FHIR's `Observation`+`VisionPrescription` shape — a significant, non-backward-compatible data-model change (research doc §4.1, "Next steps" item 1)
- [ ] If pursued: scoping conversation → background-agent build with migration + verification → living-spec update (item 2)
- [ ] SNOMED-CT/LOINC/ICD-10-CM real code-set integration — the ICD-10 auto-suggestion slice in §0 above is explicitly a narrow, hardcoded-lookup precursor to this, not a substitute for it (§4.4)
- [ ] DICOM device integration — a background C-STORE SCP listener service to pull autorefractor/OCT/visual-field device data directly into exam fields (§4.3)
- [ ] IHE GEE-structured encounter workflow (Patient Check-In → Technician Pre-Test → Doctor Exam → Assessment & Plan → Optical/Billing Out) as a first-class UI flow, rather than today's single exam-entry form (§4.2)

---

## 5. Appointment Scheduling Module — remaining gaps (spec §26.10, §36.5)

- [ ] `AppointmentTypeVersion` `created_by`/`updated_by` attribution — `Appointment` itself got this in v2.7; the type-version side was explicitly out of scope for that round (§36.5 item 10)
- [ ] Visual **Resource Schedule grid view** — `Resource`/`AvailabilityTemplate` data already exists and is enforced; no grid UI was ever built (§27.6, §31.3, §36.5 item 12)
- [ ] Room/lane/device resource conflict enforcement extended to a resource-picker UI on the booking form itself (today, resource assignment is automatic based on type requirements — no manual override UI, §31.3)
- [ ] Calendar click-to-create does not itself pre-check availability before opening the form (§18.2 item 7, still open per that item's own note)

---

## 6. Clinical Workflow Gaps (spec §18.2, §37.6)

- [ ] Clinical records (exams, prescriptions) **cannot be edited, signed, corrected, or appended** — both are create-only today, confirmed repeatedly this session (§18.2 item 4). This is a foundational gap for real clinical use: no draft → sign → lock → amend lifecycle exists at all.
- [ ] Appointment and exam records are not explicitly linked (§18.2 item 1) — worth re-verifying current truth before treating as still-open, since significant appointment-module work has happened since this was written
- [ ] Provider records cannot be managed in the application (no add/edit provider UI) — re-verify current truth (§18.2 item 3)
- [ ] Prescription relationships not validated for patient/provider/exam consistency (§18.2 item 2)
- [ ] Prism/base omitted from normal and printable prescription displays; contact-lens values omitted from normal prescription detail (§18.2 items 8-9) — re-verify against the v2.10 Lens Design & Follow-Up work, which may have already narrowed this

---

## 7. Security & Compliance

- [x] **CSRF protection. Done, v2.19** — a pre-existing, long-tracked gap, present in every version's open-gaps list (spec §15.1, §26.10 item 4, §36.5 item 3, §37.6). See baseline spec §37.7: a session-bound synchronizer token verified on all 27 POST routes, delivered via a JS-injected hidden field, plus this app's first server-side secret (`SECRET_KEY`).
- [ ] Down-migration/rollback capability in the migration runner — it only ever adds, never reverses (§25.15, §36.5 item 4)
- [ ] Per-record "who changed this specific clinical/administrative field" audit trail, beyond `AuthAuditEvent`'s authentication/access-event scope (§37.1, §37.6, §36.5 item 1's note)
- [ ] Record-level authorization (e.g. restricting a provider to only their own patients) — current model is role-level only (§37.6)
- [ ] MFA/SSO, self-service password reset, password-complexity policy beyond a sane minimum, account lockout/rate-limiting — all explicitly scoped out of the v2.4 auth build as "solid baseline, not enterprise list" (§37.6); revisit only if requirements change

---

## 8. Practice-Management Placeholders (spec §27.6, §35.3)

Deferred during the v1.5 competitive-review round, never revisited since:

- [ ] Full optical product/inventory data model behind the Catalog placeholder (overlaps with §3 above's inventory item — reconcile scope if both are picked up)
- [ ] Real insurance-claim submission/tracking behind the Claim Management placeholder (overlaps with §2 above — same billing-domain gate applies)
- [ ] Payer-specific bulk-authorization workflows (e.g. VSP-style) — deferred indefinitely pending any insurance-eligibility integration at all

---

## 9. Go-Live Prerequisites (spec §38.6)

Tracked here for visibility; the authoritative detail lives in the spec's go-live notice and §38.

| # | Prerequisite | Status |
| --- | --- | --- |
| 1 | Real authentication, authorization, and audit logging | **Done** (v2.4) |
| 2 | Compliant hosting under a signed BAA (Vercel, Neon, Cloudinary) | **Open** — tracked in [GitHub issue #2](https://github.com/ooWOWZERoo/npvehr/issues/2); a legal/procurement action, not an engineering task |
| 3 | Encryption in transit and at rest | **Substantially done** — in-transit verified directly; at-rest reasoned from standard managed-provider practice, not independently re-confirmed against current vendor terms |
| 4 | Backup and disaster recovery, tested and documented | **Done** — Neon PITR, live-tested; the one caveat is scope (Cloudinary photos aren't covered by a Neon restore) |

**Do not use this application with real patient data until prerequisite 2 is closed and the user's compliance counsel confirms readiness** — this is unchanged by anything else in this backlog.

---

## 10. Product Quality / UX (spec §18.3)

- [ ] Incomplete form-label association and other accessibility issues — no full audit has been performed
- [ ] No user-friendly validation or confirmation messages, including for photo-upload failures
- [ ] No pagination, advanced search, filters, or large-data handling on any list screen (patients, appointments, admin lists) — spec §36.5 item 13 also names this
- [ ] Dependencies specify minimum versions only (`>=`), no upper bounds or lock file — reduces build reproducibility
- [ ] Client's final logo asset still not supplied; navigation/print header show a placeholder mark

---

## 11. Testing & QA

- [ ] Expand the Playwright suite beyond smoke-level coverage (currently: login/logout/auth-redirects, main-nav-destinations-render, plus the Visit Focus toggle and composer behaviors added this session) toward the workflow-level regression coverage described in spec §20's manual checklist — most of that checklist is still not automated (§36.5 item 5's own note)
- [ ] No automated migration test suite — migrations are verified manually/via synthetic-database checks each round, not as a standing automated test (§18.3 item 7's note)

---

## Notes on stale items

A few `§18.2`/`§18.3` items above (record linkage, provider management, prescription display fields) were written early in this project's spec history and may have been partially superseded by later work (the Appointment Scheduling Module, the v2.10 Lens Design fields) without the spec's own gap-list being re-audited against them — each is flagged above with a "re-verify" note rather than assumed still fully accurate. Confirm current truth before scoping work against them.

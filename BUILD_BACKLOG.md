# New Path Vision EHR — Master Build Backlog

**Status:** Living tracking document. **Baseline as of:** spec v2.40 / research doc v2.24 (2026-09-24).

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
- [x] **CSRF protection** — a long-tracked security gap (§7). **Done, v2.19** — see baseline spec §37.7.
- [x] **Per-patient document storage + Problem List, first slice** — prompted by a gap analysis against a real visit-summary document export. **Done, v2.20** — see baseline spec §39, research doc §8.
- [x] **Pupil exam fields** — next item picked from §12's visit-summary gaps. **Done, v2.21** — see baseline spec §40, research doc §8.1.
- [x] **Motility and confrontation visual field fields** — next item picked from §12's visit-summary gaps. **Done, v2.22** — see baseline spec §41, research doc §8.2.
- [x] **Anterior Segment / Dry Eye split** — user request: separate the two, fully scoped. **Done, v2.23** — see baseline spec §42, research doc §8.3. `AnteriorSegmentAssessment` renamed to `DryEyeAssessment` (no data change); a real, new structural Anterior Segment dashboard built alongside it. Also resolves §12's "conjunctiva/anterior-chamber/iris as discrete slit-lamp structures" item.
- [x] **Visit Focus activation redesign** — user request: status dots, real accordions, a sticky chip row, prototyped first then built into the real form. **Done, v2.24** — see baseline spec §43. Layered on top of the existing chip/hidden-attribute mechanism; no route or dashboard-field changes.
- [x] **ICD-10 coverage expansion for Anterior Segment / Dry Eye / Pre-Post-Op** — user request: confirm the composer keeps auto-populating for the dashboards added since v2.15, and bring the ICD-10 lookup up to date with as complete a set of diagnosis/aftercare codes as today's structured fields support. **Done, v2.25** — see baseline spec §44, research doc §7.2. Verified codes added for pterygium, pinguecula, three age-related cataract subtypes, dry eye syndrome, and cataract-extraction aftercare status; `diagnosis_codes` now aggregates every active Visit Focus section's code(s), not just Refractive's.
- [x] **Assessment & Plan composer rebuild: styles, structured plan, ICD-10 validation, smart merge** — user request: a dedicated, tested "click-to-autofill A&P engine" core module with bulleted clinical fragments (not narrative filler), a Narrative/Abbreviated style toggle, Meds/Testing/RTC plan structure, ICD-10 laterality + glaucoma 7th-character-staging validation, and a manual-edit-preserving merge. **Done, v2.26** — see baseline spec §45. New `ehr/services/ap_composer.py` (18 unit tests) is the reference the live form's JS composer mirrors; new `GlaucomaTracking.glaucoma_stage` field makes the staging rule real.
- [x] **Calendar & Appointments UX Overhaul, Phase 1 (grid calendar core)** — user request: a deep-dive rethink of calendar/scheduling UX. **Done, v2.27** — see §5a for the full gap map and 4-phase roadmap, baseline spec §46.
- [x] **Calendar & Appointments UX Overhaul, Phase 2 (Waitlist Management)** — new `WaitlistEntry` model, per-patient tab, staff-facing global queue, matching entries surfaced on appointment cancellation. **Done, v2.28** — see §5a, baseline spec §47.
- [x] **Calendar & Appointments UX Overhaul, Phase 3 (Automated Confirmations & Reminders)** — mock-only send interface, patient opt-in fields (default off), booking-time confirmation, `CRON_SECRET`-gated hourly reminder scan, staff-facing audit log. **Done, v2.29** — see §5a, baseline spec §48.
- [x] **Calendar & Appointments UX Overhaul, Phase 4 (Online Patient Self-Booking)** — a second, patient-facing application surface with its own passwordless magic-link auth; self-service book/cancel/reschedule reusing the staff conflict-rule engine; per-type `patient_bookable` opt-in for staff. **Done, v2.30** — see §5a, baseline spec §49. This closes out the 4-phase Calendar & Appointments UX Overhaul roadmap.
- [x] **Waitlist auto-notify** — the last deferred item from Phase 2/3: cancelling an appointment now auto-notifies every matching waitlist entry (mock send, opt-in gated), with staff-visible audit trail. **Done, v2.31** — see §5a, baseline spec §50.
- [x] **Phase 4 portal follow-ups (round 1)** — reschedule provider/type change, configurable self-service cutoff, login-link rate-limiting, waitlist self-service, and a patient-facing clinical data view (visit summaries, prescriptions, documents, each view audited). **Done, v2.32** — see §5a, baseline spec §51.
- [x] **Scheduling slot/duration reconciliation** — user question prompted a rethink of how appointment durations relate to offered start times. Configurable slot granularity (practice default + per-provider override) filtering an always-fine-grained, always-correct conflict check -- never lets a longer exam get squeezed into a shorter gap, matching the scenario asked about. Also fixed a real bug found along the way: `find_open_slots` never checked room/resource conflicts, only provider availability, so a slot could be offered that then failed at actual booking. **Done, v2.33** — see baseline spec §52.
- [x] **New Exam follow-up units** — user request: Day/Week/Month/Year options on the New Exam form's follow-up field (previously weeks-only), plus renaming "New Eye Exam" to "New Exam" for consistency with every other link to the page. **Done, v2.34** — see baseline spec §53.
- [x] **Chief-complaint triage + E/M-level suggestion (Phase 1 of 4)** — user request describing a full optometry-EHR workflow (chief-complaint-driven exam-type triage, MDM-based E/M coding, CPT mapping + split billing across two patient flows, diagnostic-order tracking, and a chronic-disease look-back alert engine). None of the underlying billing/orders infrastructure existed; planned as 4 independently-shippable phases (see §0a below), of which this round builds Phase 1 only -- pure client-side decision support, never transmitted or submitted anywhere. **Done, v2.35** — see baseline spec §54.
- [x] **CPT mapping + two-flow billing preview (Phase 2 of 4)** — curated CPT catalog + diagnostic-test mapping, a per-patient insurance-plan table (vision + medical can coexist), the `EyeExam`-to-`Appointment` link that never existed before, a check-in step selecting the billing flow, and a read-only split-invoice preview card. Still staff-facing decision support only -- no claim is ever generated or transmitted. **Done, v2.36** — see baseline spec §55.
- [x] **Diagnostic order tracking (Phase 3 of 4)** — a real `DiagnosticOrder` lifecycle (`ordered → scheduled → in_progress → completed/cancelled`), wired to the Glaucoma dashboard's existing diagnostic-orders checkboxes, with a Pending Diagnostic Orders card and one-click Mark Complete/Cancel on the patient overview tab. **Done, v2.37** — see baseline spec §56.
- [x] **Look-back & clinical alert engine (Phase 4 of 4)** — no new schema; ambient `.alert-warning`/`.alert-info` banners (never a blocking modal) for outstanding diagnostic orders and overdue chronic-condition testing (glaucoma, Plaquenil monitoring), each with one-click resolution, on the patient overview tab and New Exam form header. **This closes out the 4-phase chief-complaint/CPT/billing-flow plan.** **Done, v2.38** — see baseline spec §57. Pick up the follow-up refinements logged in §0a below, the Phase 3 real-vendor follow-up (a different "Phase 3", from the Calendar overhaul), patient self-registration, or another item from the sections below.

### 0a. Chief-Complaint Triage, E/M Coding, CPT Mapping, Orders & Look-Back Alerts (user request, 2026-09-23) — closed out, v2.38

Four-phase roadmap from a single large user request; each phase is independently shippable and gets its own explicit go-ahead before building (not all four in one pass). Nothing in any phase transmits, submits, or persists an actual insurance claim -- this app has no billing/claims infrastructure at all today and is explicitly documented as not for use with real patient data; every billing-shaped piece is staff-facing decision support or an on-screen preview only.

- [x] **Phase 1 — Chief-complaint triage + E/M-level suggestion.** Keyword scan on the chief complaint suggests an exam type; a 3-factor MDM heuristic (problems/data/risk, with a hard Rx-management-implies-Moderate-risk trigger) suggests an E/M code (99212/99213/99214). Both editable/overridable, never billed. **Done, v2.35** — see baseline spec §54.
- [x] **Phase 2 — CPT mapping + two-flow billing preview.** New `cpt_codes` catalog, `patient_insurance_plans` table (vision + medical can coexist), `EyeExam.appointment_id` (finally linking clinical and scheduling sides), `Appointment.visit_flow`, a check-in step to select Flow A (routine vision-plan) vs. Flow B (medical, with 92015 refraction billed separately to the patient), and a read-only split-invoice preview card explicitly labeled "not a submitted claim." **Done, v2.36** — see baseline spec §55.
- [x] **Phase 3 — Diagnostic order tracking.** A real `DiagnosticOrder` table with an `ordered → scheduled → in_progress → completed/cancelled` lifecycle (today's `AppointmentTest` is scheduling-only, no clinical lifecycle), wired to the Glaucoma dashboard's existing diagnostic-orders checkboxes, with a pending-orders card on the patient workspace. **Done, v2.37** — see baseline spec §56.
  - [ ] Follow-up refinement: deep-link order completion to the originating Visit Focus dashboard section when the ordered test maps to one (not built -- no natural per-test-to-section mapping exists yet).
  - [ ] Follow-up refinement: a header-level pending-order count badge on the patient-context strip, alongside the existing allergy flag (needs `_workspace_ctx` to compute the count, used by every workspace-tab route).
- [x] **Phase 4 — Look-back & clinical alert engine.** No new schema -- pure queries over Phase 3's orders table and the existing Problem list, using a small hardcoded per-diagnosis testing-interval matrix (glaucoma, Plaquenil monitoring to start) to flag overdue testing as an ambient banner (never a blocking modal), with one-click resolution (an outstanding-order alert completes via Phase 3's existing route; an interval-due alert creates a fresh order via a new `quick-order` route). **Done, v2.38** — see baseline spec §57.
  - [ ] Follow-up refinement: additional condition profiles beyond glaucoma/Plaquenil (AMD/diabetic retinopathy, keratoconus, etc., from the original request's fuller matrix) -- straightforward to add in the same `CONDITION_PROFILES` shape.
  - [ ] Follow-up refinement: deep-link an interval-due alert's "Order Now" action to a specific Visit Focus dashboard section (same open question as the Phase 3 order-completion deep-link above).

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
- [~] Visual **Resource Schedule grid view** — subsumed by the Calendar & Appointments UX Overhaul below (Phase 1's board view covers per-provider scheduling; a dedicated non-provider Resource grid, e.g. rooms/lanes/devices as their own board, is still open)
- [ ] Room/lane/device resource conflict enforcement extended to a resource-picker UI on the booking form itself (today, resource assignment is automatic based on type requirements — no manual override UI, §31.3)
- [ ] Calendar click-to-create does not itself pre-check availability before opening the form (§18.2 item 7, still open per that item's own note)
- [x] **`publish_new_version` doesn't carry resource requirements forward** (found while verifying the slot/duration reconciliation round, baseline spec §52.4): it carried a type's color rules onto a newly published version but left `AppointmentTypeResourceRequirement` rows attached to the now-inactive previous version, so republishing a type that needs a room/resource silently dropped that requirement. Resolved: there is no admin UI anywhere to view or edit a resource requirement, at publish time or otherwise, so there's no legitimate workflow a blanket carry-forward could break — same treatment as color rules just above it in that route. **Done, v2.39** — see baseline spec §58.

---

## 5a. Calendar & Appointments UX Overhaul (user request, 2026-09-16) — in progress

User asked for a deep-dive rethink of the calendar/scheduling UX against ten specific features. Gap map against the code as of v2.26 (not assumptions): color-coding (`AppointmentTypeColorRule`), day/week/month views, and conflict/rule checking (`_apply_scheduling_rules`, provider + resource double-booking, override+reason+audit) were **already built and substantially mature**; multi-provider/location grids, drag-and-drop, hover cards, waitlist, online self-booking, and automated reminders were **not built at all**. Full gap map recorded in this session's transcript; phased roadmap below.

- [x] **Phase 1 — Grid calendar core.** FullCalendar (MIT core, vendored locally rather than CDN-loaded) replaces the server-rendered day/week/month list views with a real time-slot grid: drag-and-drop reschedule (reuses the existing `POST /appointments/{id}/reschedule` endpoint and its `_apply_scheduling_rules` conflict check — a drag that would double-book is blocked/reverted exactly like the form is today), hover-card popovers, and extended filters (room, has-notes — no payment-status filter, since no payment/billing data model exists yet, per §2's gate). Multi-provider/room view ships as a **free workaround**: side-by-side single-provider FullCalendar instances sharing one time axis, not FullCalendar Premium's paid resource-timeline plugin (evaluated and explicitly declined pending real usage feedback). **Done, v2.27** — see baseline spec §46. Also fixed a pre-existing, previously-untriggered 422 bug (empty-value filter selects) affecting every board/day/week/calendar route.
- [x] **Phase 2 — Waitlist.** New `WaitlistEntry` model + patient-workspace tab + staff-facing global queue; cancelling an appointment surfaces matching entries to staff (rescheduling's vacated slot is not handled -- explicitly scoped out, see spec §47.2). No auto-notify yet (needs Phase 3's messaging infra). **Done, v2.28** — see baseline spec §47.
- [x] **Phase 3 — Automated confirmations/reminders.** Scoped by explicit decision: mock-only sends (no real Twilio/SendGrid vendor/credentials this round, behind a swappable `ehr/services/notifications.py` interface), `Patient.sms_opt_in`/`email_opt_in` opt-in fields defaulting `False`, and appointment reminders/confirmations only (waitlist auto-notify still deferred, see below). Booking-time mock confirmation; `CRON_SECRET`-gated hourly scan (`GET /appointments/reminders/run`, 24h lookahead) wired via `vercel.json`'s `crons` entry; staff-facing audit log (`GET /appointments/reminders`). **Done, v2.29** — see baseline spec §48.
- [x] **Waitlist auto-notify** (deferred from Phase 2/3): cancelling an appointment now automatically sends a (mock) notice to every matching active waitlist entry, gated per-channel on that patient's own opt-in -- wired into every cancellation path (staff status change, staff edit form, patient portal). New `WaitlistNotification` model, deduped on (waitlist_entry_id, appointment_id, channel). Staff visibility: a "Notified" column on the appointment detail page and a "Recent Waitlist Notifications" section on the staff waitlist queue. **Done, v2.31** — see baseline spec §50. This closes the last item in this list.
- [ ] **Real SMS/email vendor integration** (Phase 3 follow-up, blocks one Phase 4 follow-up too): swap the mock `send_sms`/`send_email` in `ehr/services/notifications.py` for a real vendor (Twilio for SMS, SendGrid or similar for email) once one is chosen. Two call sites currently only log what would have been sent: (1) appointment confirmations/reminders and waitlist-opening notices (`send_appointment_notice`/`send_waitlist_opening_notices`, Phase 3 + the waitlist auto-notify item above); (2) the patient portal's magic-link login (`send_portal_login_link`, Phase 4, §49) — the portal is not actually usable by a real patient until this lands, since today's "link" only appears in a dev-only on-page fallback, never a real inbox. Needs, before going live with real sends: vendor account + API credentials (stored as env vars, following the `SECRET_KEY`/`CRON_SECRET` pattern in `ehr/env_info.py`, never committed); a decision on retry/backoff behavior for a real "failed" status (today's mock never fails, so `AppointmentReminder`/`WaitlistNotification`/login-token rows have never exercised that path); a security/compliance pass appropriate to real outbound SMS/email carrying PHI (e.g. whether reminder text may name the appointment type/diagnosis-adjacent detail, or must stay generic); and confirming carrier/deliverability requirements (SMS short-code/10DLC registration, sender domain SPF/DKIM for email) are practice-side setup, not app code.
- [x] **Phase 4 — Online patient self-booking.** A second, patient-facing application surface (`ehr/routes/portal.py`, `/portal/*`) with its own passwordless (magic-link) auth entirely separate from staff sessions; self-service book/cancel/reschedule reusing the exact staff conflict-rule engine (no conflict/duration override, automatic relationship classification, ownership checks, a 24h self-service cutoff window); per-type `patient_bookable` opt-in (off by default) for staff. **Done, v2.30** — see baseline spec §49. This closes out the 4-phase roadmap.
- [x] **Phase 4 portal follow-ups (round 1)**: reschedule can now change provider/type (not just time); the self-service cutoff is a staff-configurable `PortalSettings` setting (`admin/scheduling/portal-settings`) instead of a hardcoded constant; login-link requests are rate-limited (3 per 15 min per matched patient); waitlist self-service (`/portal/waitlist`); a patient-facing clinical data view (visit summaries, prescriptions, documents at `/portal/records/*`, each view logged to `PortalAccessAuditEvent`). **Done, v2.32** — see baseline spec §51.
- [ ] **Portal follow-ups still open**: patient self-registration (today the portal only authenticates existing chart-matched emails -- needs its own identity-verification design); a staff-facing audit page surfacing `PortalAccessAuditEvent` (recorded but not yet displayed anywhere); edit of a patient-created waitlist entry after creation (cancel-and-recreate only today). (The real-email-vendor item that used to live here is consolidated into the Real SMS/email vendor integration item above, since it's the same underlying `ehr/services/notifications.py` swap.)
- Explicitly **not** rescheduled by this round: EHR/billing integration (Appointment→Exam linkage still needs re-verification per the item above; real billing/claims stays gated behind §2's clearinghouse/compliance/BAA prerequisites, unchanged by this plan).

---

## 6. Clinical Workflow Gaps (spec §18.2, §37.6)

- [ ] Clinical records (exams, prescriptions) **cannot be edited, signed, corrected, or appended** — both are create-only today, confirmed repeatedly this session (§18.2 item 4). This is a foundational gap for real clinical use: no draft → sign → lock → amend lifecycle exists at all.
- [ ] Appointment and exam records are not explicitly linked (§18.2 item 1) — worth re-verifying current truth before treating as still-open, since significant appointment-module work has happened since this was written
- [ ] Provider records cannot be managed in the application (no add/edit provider UI) — re-verify current truth (§18.2 item 3)
- [ ] Prescription relationships not validated for patient/provider/exam consistency (§18.2 item 2)
- [ ] Prism/base omitted from normal and printable prescription displays; contact-lens values omitted from normal prescription detail (§18.2 items 8-9) — re-verify against the v2.10 Lens Design & Follow-Up work, which may have already narrowed this
- [x] **`follow_up_unit` (v2.34) has the same latent edited-flag ordering bug fixed for the new suggestion fields in v2.35** (baseline spec §54.2): a `<select>` fires `input` before `change`, and the generic form-recompute wiring listens for both, so an edited-flag set only on `change` lets the `input`-triggered recompute fire first and silently revert a clinician's manual unit selection. Fixed the same way (also set the flag on `input`); a new regression test confirms it (verified to fail without the fix). **Done, v2.40** — see baseline spec §59.

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
- [ ] App-wide horizontal overflow at ~400px width — `document.documentElement.scrollWidth` exceeds `clientWidth` on every page tested (dashboard, exam detail, exam form), including pages with no wide tables at all, so it's in the base layout/sidebar chrome, not any one page's content. Found incidentally during the v2.24 Visit Focus round (spec §43.3); not investigated or fixed there since it predates and is unrelated to that work.

---

## 11. Testing & QA

- [ ] Expand the Playwright suite beyond smoke-level coverage (currently: login/logout/auth-redirects, main-nav-destinations-render, plus the Visit Focus toggle and composer behaviors added this session) toward the workflow-level regression coverage described in spec §20's manual checklist — most of that checklist is still not automated (§36.5 item 5's own note)
- [ ] No automated migration test suite — migrations are verified manually/via synthetic-database checks each round, not as a standing automated test (§18.3 item 7's note)

---

## 12. Remaining Visit-Summary Gaps (research doc §8, v2.20)

A real visit-summary document export prompted a full component-by-component gap analysis this round; per-patient document storage and a Problem List first slice were picked from it and built (§0 above, spec §39). Everything else that document needs remains here, unscoped:

- [x] **Pupil exam — size/reactivity/APD per eye, light/dark/near measurements. Done, v2.21** — see baseline spec §40, research doc §8.1. Ten new nullable columns on `EyeExam` (flat columns like Visual Acuity/Slit Lamp/Fundus, not a new Visit Focus dashboard).
- [x] **Motility and confrontation visual fields as structured OD/OS data. Done, v2.22** — see baseline spec §41, research doc §8.2. Four new nullable columns on `EyeExam` (flat columns, same treatment as pupil exam fields), distinct from the pre-existing `cover_test` field (ocular alignment, a different clinical concept).
- [x] **Conjunctiva / anterior chamber / iris as discrete slit-lamp structures. Done, v2.23** — see baseline spec §42, research doc §8.3. Built as part of a full structural Anterior Segment dashboard (also covering cornea pathology and lens/cataract grading), split out from the old "Anterior Segment / Dry Eye" dashboard which was entirely dry-eye content.
- [ ] Vitreous as a discrete fundus structure, and a numeric CD ratio on the general exam (today it only exists on `GlaucomaTracking`)
- [ ] Structured review of systems (the source document's large systemic-symptom checklist)
- [ ] Structured social history (alcohol/tobacco screening) — no fields exist on `Patient` at all
- [ ] Diagnostic-imaging order + structured result tracking (fundus photos, OCT) — no order/result model exists; Order Management remains a placeholder (§3 above)
- [ ] E-signature / sign-lock-amend workflow, and "staff present at this visit" attribution beyond the single `Provider` on an exam — both already tracked (§6 above, spec §18.2 item 4/§37.6)
- [ ] Actual PDF rendering — this app has zero PDF-generation library; `prescriptions/print.html` relies entirely on the browser's native print dialog

---

## Notes on stale items

A few `§18.2`/`§18.3` items above (record linkage, provider management, prescription display fields) were written early in this project's spec history and may have been partially superseded by later work (the Appointment Scheduling Module, the v2.10 Lens Design fields) without the spec's own gap-list being re-audited against them — each is flagged above with a "re-verify" note rather than assumed still fully accurate. Confirm current truth before scoping work against them.

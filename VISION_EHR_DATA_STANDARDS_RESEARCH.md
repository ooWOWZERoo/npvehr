# Vision/Eye Care EHR Data Standards — Research Notes

**Status:** Reference material. **Section 4.2's first narrow slice — the habitual/manifest/cycloplegic refraction-type distinction — was implemented in v2.8** of the baseline spec (see that document's §12.6a). Everything else here (FHIR resource modeling, IHE GEE encounter workflow, DICOM device integration, SNOMED/LOINC/ICD-10 terminology) remains unimplemented. Saved for review and planning in a future session — see "Next steps" at the bottom.

**Context:** Generic medical EHR specifications are insufficient for eye care because of strict per-eye laterality (OD/OS/OU), specialized measurement matrices (e.g., manifest vs. cycloplegic refraction), and a medical/retail-vision billing split. This note collects the dominant existing specifications so any future architectural rework of New Path Vision EHR's data model can align with them from the start, rather than requiring a rewrite later.

---

## 1. Functional Profiles & Frameworks

- **HL7 Eye Care Functional Profile (Eye Care-FP)** — baseline functional criteria for documenting adult and pediatric outpatient eye exams; standardizes how ophthalmic data should be generated, managed, and stored.
- **AAO Medical Information Technology Committee (MITC) Requirements** — a published checklist of 17 "essential" and 6 "desirable" EHR features for eye care: mapping the "vital signs of the eye" (Visual Acuity, IOP), integrating hand-drawn sketches of fundus/cornea, and seamless clinic-to-operating-room documentation handoff.

## 2. Interoperability & Data Models

- **HL7 FHIR Eye Care Implementation Guide** (`build.fhir.org/ig/HL7/fhir-eyecare-ig`) — defines ophthalmic FHIR extensions, including `Ocular Anatomical Location` (SNOMED-coded, pinpoints granular eye structures) and profiles for `Visual Acuity` and `Intraocular Pressure (IOP)`.
- **IHE Eye Care Content Profiles** (ihe.net/ihe_domains/eye_care) —
  - **General Eye Evaluation (GEE)**: structure of the document collected during a routine/comprehensive eye exam.
  - **Eye Care Displayable Report (ECDR)**: capturing/retrieving imaging reports (e.g., Visual Fields) as DICOM encapsulated PDFs.
- **DICOM Supplements for Ophthalmology** — dedicated DICOM extensions so imaging/numerical device data syncs natively to the visit note (e.g., Supplement 115: Ophthalmic Corneal Topography Mapping; Supplement 146: Structural Matching Image).

## 3. Registry & Quality Reporting

- **AAO IRIS® Registry Integration Specifications** — one of the largest specialty clinical data registries; requires structured export fields (automated ICD-10 staging, CPT coding for refractions vs. dilated exams, MIPS quality measures).

---

## 4. Architectural Guidance for Building From Scratch

### 4.1 Core data model: FHIR & C-CDA

Don't invent custom JSON schemas for eye data — build on:
- **HL7 "Eyes on FHIR" Initiative**
- **HL7 Specialty Eyecare CDA Guide** (`build.fhir.org/ig/HL7/cda-eyecare`)

Key FHIR resources:
- **`VisionPrescription`** — built-in FHIR resource modeled for spectacles/contacts; natively handles sphere, cylinder, axis, prism, add power.
- **`Observation` + Ocular Anatomical Location extension** — for IOP, Visual Acuity, etc.; nests a laterality qualifier (Right eye: `18944008`, Left eye: `18949003`, SNOMED codes).

### 4.2 Visit blueprint: IHE GEE workflow

```
Patient Check-In → Technician Pre-Test → Doctor Exam → Assessment & Plan → Optical/Billing Out
```

Encounter data should group into these functional blocks:

| Block | Core sub-fields |
| --- | --- |
| History & Complaints | Chief complaint, visual demands, systemic conditions (Diabetes, Hypertension), family history (Glaucoma, Macular Degeneration) |
| Visual Acuity (VA) | Uncorrected (sc), Distance, Near, Pin-hole (PH), Best-Corrected (BCVA); units toggle Snellen/LogMAR/Decimal |
| Refraction Matrix | Three separate steps: Habitual (current glasses), Manifest (subjective refinement), Cycloplegic (with dilating drops) |
| Tonometry (IOP) | Method (Goldmann Applanation, Tono-Pen, iCare), Value (mmHg), Time of day (critical for glaucoma tracking) |
| Slit Lamp (Anterior) | Lids/Lashes, Conjunctiva, Cornea, Anterior Chamber (depth/cells/flare), Iris, Lens (Nuclear sclerotic, Cortical, Posterior subcapsular cataract staging) |
| Fundus (Posterior) | Optic Nerve (cup-to-disc ratio horizontal/vertical), Macula, Vasculature, Periphery |

### 4.3 Hardware integration: DICOM

Devices to eventually integrate with: autorefractors, lensmeters, visual field machines, OCT scanners.
- Reference: DICOM Ophthalmology Supplements (115, 146, etc.)
- Implementation strategy: a background **DICOM C-STORE SCP listener service** to intercept/parse incoming device files, pulling values (e.g., autorefractor readings) directly into manifest refraction fields without manual entry.

### 4.4 Terminology / code sets

Avoid free-text for clinical findings — map to:
- **SNOMED-CT** — findings (e.g., "Nuclear cataract", "Dry age-related macular degeneration")
- **LOINC** — procedures/measurements (e.g., `70936-0` = Ophthalmic manifest refraction panel)
- **ICD-10-CM** — laterality-specific 7-character codes (e.g., `H40.1111` = primary open-angle glaucoma, right eye, mild stage)

---

## How this relates to New Path Vision EHR today

The current data model (`EyeExam`, `Refraction`, `Prescription` in `ehr/models/database.py`) is a simplified, mostly free-text/flat-field representation — it does **not** currently align with most of the above (no FHIR resources, no SNOMED/LOINC/ICD-10 coding, no DICOM device integration, no IHE GEE-structured encounter workflow). **One piece now does align: the habitual/manifest/cycloplegic refraction split** (item 3 in "Next steps" below), implemented in the baseline spec's v2.8 (§12.6a) — `Refraction.refraction_type` already existed on this table but was previously write-only, always hardcoded to `manifest`; an exam can now carry an independent, optionally-present row for each of the three types. Everything else remains a general gap, documented in the living spec (`NEW_PATH_VISION_EHR_BASELINE_PRODUCT_DEFINITION_AND_SPECIFICATION.md`, §36.5 item 11).

## 5. Additional Clinical Dashboard Requirements (reviewed from an uploaded requirements document, v2.9)

**Source:** an uploaded requirements document (`NPVEHR_Reqs1.docx`) reviewed and reconciled into this file. It reads as AI/consultant-style output aimed at a generic modern EHR stack, not written against this specific codebase — its code samples target a React+TypeScript SPA and an async SQLAlchemy 2.0 backend with Postgres-only column types (`ENUM`, `ARRAY`, `TIME WITH TIME ZONE`). This app is server-rendered FastAPI+Jinja2 with synchronous SQLAlchemy and a migration runner that must work identically on SQLite (dev) and Postgres (prod, see `ehr/db/migrations.py`). **None of the source document's literal code is usable as-is.** What follows keeps only the clinically meaningful part — the field lists and value sets — translated into this app's own conventions: a Postgres `ENUM` becomes a plain `String` column with the allowed values noted here rather than enforced at the DB level (the pattern already used for `Refraction.refraction_type`, `AppointmentTypeColorRule.color`, etc.); a Postgres `ARRAY` becomes either a delimited string or a child table, decided per-field as this gets scoped for real; React/TypeScript/async-Pydantic code is dropped entirely.

Per the same reasoning already applied to §4.1 above, this section's schemas are **not** mapped to FHIR resources yet — that mapping is deferred for the same reason item 1 in "Next steps" below is still open. Nothing in this section is implemented; it is target-state requirements only.

### 5.1 Comprehensive Refractive & Exam dashboard — **implemented in v2.10**

Extends the existing `EyeExam`/`Refraction`/`Prescription` tables (§12.5–§12.7 of the baseline spec) rather than replacing them — the source document's "Assessment" fields are new, but its "Plan" (spectacle Rx) fields mostly already exist on `Prescription`. **Built in v2.10** exactly as scoped below: plain nullable `VARCHAR` columns (migration `015_refractive_assessment_and_plan`), new form sections in `exams/form.html`/`prescriptions/form.html` using the existing `test-chip` checkbox-group pattern for multi-value fields, and display on both detail pages plus the printed Rx. Diagnosis coding stayed free-text as planned — no ICD-10 lookup table.

| Field | Translated type | Notes |
| --- | --- | --- |
| Primary Refractive Dx | String (multi-value, comma-delimited or a child table) | Myopia / Hyperopia / Astigmatism / Presbyopia / Anisometropia / Emmetropia |
| Laterality | String | `OD` / `OS` / `OU` — matches this app's existing per-eye field-pair convention rather than a single laterality flag |
| Stability | String | Stable / Progressing / Improving |
| Secondary Findings | String (multi-value) | Amblyopia, Strabismus history, Cataract suspect, Suspect Glaucoma |
| Diagnosis code | Existing `EyeExam.diagnosis_codes` free-text field already covers this; an ICD-10 code table/auto-populate is future terminology-server work (§4.4), not new here |
| Lens Type | String | Single Vision / Bifocal / Trifocal / Progressive / Office-Computer |
| Lens Material | String | CR-39 / Polycarbonate / Trivex / Hi-Index 1.67 / Hi-Index 1.74 |
| Lens Treatments | String (multi-value) | Anti-Reflective Coating, Blue Light Filter, Transitions/Photochromic, Polarized |
| Recall Interval | String | 3 Months / 6 Months / 1 Year / 2 Years |
| Patient Education Tags | String (multi-value) | 20-20-20 Rule, UV Protection, Contact Lens hygiene |

**Correction (found during a v2.11-era review):** the source document's UI mockup for this dashboard also included an "Auto-Generated Clinical Note Output Summary" — a narrative sentence synthesized from the structured fields above (e.g. "Patient diagnosed with stable bilateral myopia. Spectacle Rx issued for progressive polycarbonate lenses with anti-reflective and blue-light filtering coatings..."). That idea was dropped during the v2.9 translation pass because it was framed as a React component sketch rather than a data-model concern — but the underlying concept (auto-populate the free-text `assessment`/`plan` fields from the structured chips, editable rather than locked) is stack-agnostic and was missed. See "Next steps" below.

### 5.2 Anterior Segment & Ocular Surface Disease (Dry Eye) dashboard — **implemented in v2.11**

A new encounter-scoped table, `AnteriorSegmentAssessment` (`exam_id` FK, mirroring `Refraction`'s exam-scoped child-row shape exactly rather than also carrying a redundant `patient_id`), following this app's existing pattern of `Refraction`-style child tables rather than widening `EyeExam` itself. **Built in v2.11** exactly as scoped below: migration `016_create_anterior_segment_assessments`, a new form section on the exam form (grading scales as `<select>` dropdowns, consistent with this app's existing dropdown convention rather than the source document's button-group UI), and a conditionally-shown detail-page card. This dashboard also introduced the **Visit Focus** navigation model (checkboxes at the top of the new-exam form that reveal each dashboard's Assessment & Plan section) needed once a second dashboard joined Refractive Assessment on the same form — see the baseline spec's §12.5b.

| Field | Translated type | Notes |
| --- | --- | --- |
| primary_diagnosis_code | String | e.g. `H04.123` — free text, same treatment as `EyeExam.diagnosis_codes` |
| severity | String | Mild / Moderate / Severe |
| conjunctival_injection_od / _os | String | Grading scale `0`, `1+`, `2+`, `3+`, `4+` |
| corneal_staining_od / _os | String | Same grading scale |
| mgd_expression_od / _os | String | Same grading scale (Meibomian Gland Dysfunction) |
| tbut_seconds_od / _os | Integer | Tear Break-Up Time, seconds |
| schirmer_mm_od / _os | Integer | Schirmer test, mm |
| plan_therapeutics | String (multi-value) | e.g. Preservative-Free Tears, Warm Compresses, Topical Steroid, Restasis/Xiidra |
| follow_up_interval, clinical_notes | String / Text | As elsewhere in this app |

### 5.3 Posterior Segment & Glaucoma Tracking dashboard — **implemented in v2.16**

Built as an `exam_id`-FK child row (`GlaucomaTracking`), same shape as `Refraction`/`AnteriorSegmentAssessment`, rather than the source document's `(patient_id, created_at)`-indexed patient-scoped table — trending is achieved by querying every row across a patient's exam history (joined via `EyeExam.patient_id`) instead of denormalizing `patient_id` onto this table, keeping every dashboard's linkage shape consistent. **Built in full, including the longitudinal trend view** flagged as an open scoping question in `BUILD_BACKLOG.md` — confirmed with the user to build the complete spec, not just the single-visit snapshot shape used for §5.1/§5.2.

The trend view is a new patient-workspace tab (`GET /patients/{id}/glaucoma-trend`, "Glaucoma Tracking" in the subnav) showing a table of every exam with a tracking row (newest first, each linking to its exam) plus a simple inline `<svg>` line chart of current IOP OD/OS across visits, computed server-side as plain Python (index-spaced X axis, since visit dates aren't evenly distributed; a dashed 21 mmHg reference line). No charting library or CDN dependency, consistent with this app's zero-external-JS-dependency convention. Target IOP is shown in the table only, not layered onto the chart, to keep the one visual signal (current IOP trend) clear. Each exam's own Glaucoma detail card links to this tab ("View full history →").

| Field | Translated type | Notes |
| --- | --- | --- |
| primary_diagnosis_code | String | e.g. `H40.1132` |
| target_iop_od / _os | Integer | mmHg |
| iop_current_od / _os | Integer | mmHg |
| iop_time_measured | Time (or String `HH:MM`) | IOP varies by time of day |
| iop_method | String | Goldmann Applanation / Tono-Pen / iCare |
| cup_disc_ratio_od / _os | Float | 0.00–1.00 |
| nerve_tissue_status_od / _os | String | e.g. "Healthy Rim", "Inferior thinning", "Notching" |
| oct_rnfl_average_microns_od / _os | Integer, nullable | OCT retinal nerve fiber layer thickness |
| visual_field_md_db_od / _os | Float, nullable | Visual Field Mean Deviation, dB |
| vf_reliability_od / _os | String | Reliable / Borderline / Unreliable |
| prescribed_glaucoma_meds | String (multi-value) | e.g. "Latanoprost 0.005% QHS OU" |
| diagnostic_orders | String (multi-value) | e.g. "OCT RNFL", "Humphrey VF 24-2" |
| follow_up_interval, clinical_notes | String / Text | |

### 5.4 Binocular Vision & Pediatrics (Vision Therapy) dashboard

A new table, e.g. `BinocularVisionAssessment`.

| Field | Translated type | Notes |
| --- | --- | --- |
| primary_diagnosis_code | String | e.g. `H51.11` (Convergence insufficiency) |
| phoria_distance_diopters, phoria_near_diopters | Integer | Negative = Exo, Positive = Eso |
| strabismus_present | Boolean | |
| strabismus_direction | String, nullable | Exotropia / Esotropia / Hypertropia |
| npc_break_cm, npc_recovery_cm | Float | Near Point of Convergence |
| accommodation_amplitude_od / _os | Float | Diopters |
| assigned_home_exercises | String (multi-value) | e.g. "Brock String", "Lifesaver Card" |
| therapy_session_number | Integer | e.g. session 4 of 12 |
| therapy_compliance_rating | String | Excellent / Good / Fair / Poor |
| follow_up_interval, clinical_notes | String / Text | |

### 5.5 Pre- and Post-Operative Co-Management dashboard

A new table, e.g. `SurgeryComanagementTracking`, with one row per follow-up encounter along a patient's surgical timeline (the source document models `current_milestone` as a single mutable field; a per-visit row is more consistent with this app's append-only, audit-friendly style — see §26.6/§26.9's `AppointmentAuditEvent` pattern).

| Field | Translated type | Notes |
| --- | --- | --- |
| surgical_procedure | String | Cataract Extraction with IOL / LASIK / PRK / SMILE / YAG Capsulotomy / Selective Laser Trabeculoplasty |
| operative_eye | String | `OD` / `OS` / `OU` |
| date_of_surgery | String (date) | |
| surgeon_name, co_managing_facility | String | |
| current_milestone | String | Pre-Op Clearance / Day 1 / Week 1 / Month 1 / Month 3 Post-Op / Released to Regular Care |
| best_corrected_visual_acuity | String | e.g. "20/20" |
| intraocular_pressure | Integer | mmHg |
| corneal_edema_present | Boolean | |
| corneal_edema_grading, anterior_chamber_cells_flare | String | Same `0`/`1+`/`2+`/`3+`/`4+` grading scale as §5.2 |
| surgical_flap_or_wound_status | String | e.g. "Intact, Clear, Well-Apposed" |
| steroid_taper_schedule | Text | e.g. "Pred Forte: QID x 1 week, then TID..." |
| nsaid_drops_frequency, antibiotic_drops_status | String | |
| follow_up_interval, clinical_notes | String / Text | |

### 5.6 E-prescribing, optical lab integration, and inventory — target-state only, no build attempted

The source document also describes: e-prescribing via NCPDP SCRIPT (drug identification against an RxNorm database, structured SIG codes — quantity, form factor, frequency, route, substitution flag); optical lab order transmission via ANSI Z80/VisionWeb-style APIs (frame boxing dimensions, per-eye centration/position-of-wear metrics, a JSON order payload, and validation guards such as "block transmission if a progressive lens lacks a Seg Height"); and in-house optical inventory (separate `inventory_frames` and `inventory_contact_lenses` tables with UPC/SKU lookups, stock counts, reorder thresholds, and a transactional checkout path using row-level locking).

These are all **real external integrations or a genuinely new inventory subsystem**, not internal refactors — none are implemented, and none are being attempted without actual vendor accounts/agreements, consistent with how DICOM device integration (§4.3) is already treated. They are recorded here purely as target-state requirements so a future session that does pursue e-prescribing, lab ordering, or inventory doesn't have to re-derive the field-level detail from scratch. The baseline spec's Capability Boundary (§19) has been updated to list these explicitly as not present, rather than simply never having discussed them.

## 6. Billing, Claims, and Insurance (target-state only, from a second uploaded requirements document, v2.13)

**Source:** a second uploaded requirements document (`NPVEHR_Reqs2.docx`), reviewed and reconciled here. Same character as §5's source: async FastAPI/React/Postgres-only DDL assumed throughout — translated into this app's plain-column, server-rendered conventions below, literal code dropped. The document also contained a few paragraphs of accidentally-included web-search-result blurbs (unrelated blog snippets) mixed into otherwise-genuine content; those were discarded as noise, not reconciled as requirements.

This domain — insurance eligibility, billing, claims, payments — is explicitly listed as not present in the baseline spec's Capability Boundary (§19), and carries materially higher compliance and financial stakes than any dashboard in §5: a real EDI 837 transaction or NCCI/LCD rule implemented incorrectly can cause claim rejections, compliance violations, or improper billing, not just a wrong on-screen value. **Everything below is recorded as target-state documentation only, requiring a real clearinghouse/payer relationship and compliance review before any build is attempted** — the same posture as e-prescribing/lab integration (§5.6), one step more cautious given the added regulatory weight. This app also remains marked "do not use with real patient data" (see the baseline spec's go-live notice), which alone rules out a real build of this domain for now regardless of technical readiness.

### 6.1 Billing invoice / claim line shape

| Field | Translated type | Notes |
| --- | --- | --- |
| billing_status | String | Draft / Ready for Clearinghouse / Submitted / Paid / Denied / Appealed / Pending Conflict |
| primary_insurance_payer_id, insured_policy_number | String | e.g. payer name/ID, policy number |
| prior_authorization_number | String, nullable | CMS-1500 Box 23 |
| dx_pointer_1/2/3 | String, nullable | ICD-10 codes — CMS-1500 Box 21 diagnosis pointers |
| total_charged_amount, insurance_expected_reimbursement, patient_copay_responsibility | Float | Money amounts |
| **Service line** (child rows): charge_type | String | Product Checkout / Clinical Service Procedure |
| cpt_hcpcs_code, modifier_1, modifier_2 | String | e.g. `92014`, `RT`/`LT` |
| linked_dx_pointers | String (multi-value) | Which of the invoice's diagnosis pointers this line supports |
| unit_count, unit_charge_amount | Integer / Float | |
| inventory_item_identifier | String, nullable | Links a line to a UPC/SKU (§5.6's inventory tables) for cost reporting |

### 6.2 Code-conflict rule matrices (a reusable pattern independent of the billing domain)

Two lookup tables, so coding rules can be updated as billing law changes without a code deploy:
- **CCI/mutually-exclusive-code edits**: `primary_cpt`, `conflicting_cpt`, `allowed_with_modifier_59` (Boolean), `error_message`.
- **Medical necessity / LCD**: `cpt_code`, `allowed_icd10_prefix` — which CPT codes are billable for which ICD-10 diagnosis families.

The lookup-table-over-hardcoded-Python approach itself is sound practice regardless of whether this domain is ever built — it mirrors this app's existing preference for data-driven configuration (e.g. `AppointmentTypeColorRule`) over conditional logic.

### 6.3 Checkout-block workflow (concept only)

A "Pending Conflict" status plus an `unresolved_conflicts` log (conflict type, error message, remediation suggestion, resolved flag) that blocks front-desk checkout until addressed, surfacing the specific rule violated and what fixes it (e.g. "apply Modifier -59" or "remove one of two mutually exclusive procedures"). Recorded as a workflow description, not a schema commitment — a real implementation would need this integrated with wherever checkout/payment actually happens, which doesn't exist in this app today.

### 6.4 EDI 837 (X12) and CMS-1500 generation — compliance-gated, not a formatting exercise

The source document includes a sample ASC X12 EDI 837 segment sequence and a CMS-1500 field mapping. **These are illustrative only, not a certified transaction** — matching the source document's own segment structure does not make a real, payer-accepted claim; that requires a genuine clearinghouse relationship, payer-specific companion guides, and compliance testing this project has no path to today. If this domain is ever pursued, the field-level shapes above are a reasonable starting point for the *internal* data model, but the EDI/CMS-1500 generation step itself is a distinct, separately-scoped effort requiring real business relationships, not an engineering task alone.

## 7. Reconciling a third uploaded document — conflicts with shipped work, and two compatible ideas (v2.13)

**Source:** a third uploaded requirements document (`NPVEHRReqs3.docx`). Unlike §5/§6's source documents, this one explicitly references this app's real table names (`eye_exams`, `refractions`, `anterior_segment_assessments`) and file paths (`ehr/db/migrations.py`, `tests/test_smoke.py`) — it was evidently produced with visibility into this project (e.g. screenshots or output fed to another tool), not written cold. Most of its proposals conflict with or duplicate what's already shipped rather than extending it.

### 7.1 Conflicts — documented so they aren't reintroduced later

- **Server-round-trip narrative composer.** Proposes a `/api/exams/compute-narrative-and-scrub` endpoint that recomputes Assessment/Plan text via a `fetch()` call on every field change. The v2.12 composer (baseline spec §12.5c) already does this **client-side**, instantly, with no network round-trip and no new endpoint. **Do not adopt** — the existing approach is faster and simpler for the same result.
- **Flat refraction fields on `EyeExam`.** Assumes `exam.manifest_sphere_od`-style flat columns. In reality, refraction values live in the separate `Refraction` child table keyed by `refraction_type` (`habitual`/`manifest`/`cycloplegic`, v2.8) — not flat columns on `EyeExam`. Any future work in this area must build on the real shape, not this document's assumed one.
- **Duplicate biomicroscopy table.** Proposes a new `exam_biomicroscopy_records` table for slit-lamp/fundus findings. `EyeExam` already has these as flat columns (`sl_lids_od/os`, `sl_cornea_od/os`, `sl_lens_od/os`, `fundus_disc_od/os`, `fundus_macula_od/os`, `fundus_vessels_od/os`, `fundus_periphery_od/os`) since before v1.0. A parallel table would create two sources of truth for the same data — **do not build this table**.
- **Non-conforming migration pattern.** Its sample migration function signature (`def migration_017(inspector, engine)`) doesn't match this app's actual convention (`def migration_NNN(conn)`, using the module-level `_table_exists`/`_add_column_if_missing`/`_pk_ddl` helpers in `ehr/db/migrations.py`), and its "dialect-agnostic" DDL hardcodes SQLite's `INTEGER PRIMARY KEY AUTOINCREMENT` unconditionally — which would fail on Postgres. Any future migration must follow the real pattern, not this one.
- **Conflating clinical and billing concerns.** Bundles claims-scrubbing into the same endpoint as clinical narrative generation. These should stay separate — a concrete instance of why §6 (billing) is scoped as its own future domain, not woven into the exam-entry workflow.

### 7.2 Two compatible ideas — **implemented in v2.15**

- **ICD-10 auto-suggestion.** A small lookup keyed on (diagnosis, laterality) — e.g. Myopia+OD → `H52.11`, +OS → `H52.12`, +OU → `H52.13` — to *suggest*, not force, a value into the existing free-text `diagnosis_codes` field. **Built in v2.15** exactly as scoped: extends the v2.12 composer function directly (no new endpoint), covering all six Refractive Assessment diagnoses (Emmetropia excluded — not billable). Still subject to the existing terminology-server deferral (§4.4): this hardcoded lookup for a handful of common diagnoses is much narrower than real ICD-10 code-set integration, and should not be read as satisfying that larger item.
- **Diagnosis-driven recall interval.** Vary the composer's auto-suggested follow-up interval by diagnosis — e.g. a shorter recall when "Suspect Glaucoma" is checked as a secondary finding, vs. the current flat default. **Built in v2.15**: a small rule extension to the existing Plan-composing function in `exams/form.html`, no schema change — see the baseline spec's §12.5d for full detail and verification.

## Next steps (partially started — see status notes)

1. Decide whether/when to align the `EyeExam`/`Refraction`/`Prescription` schema toward FHIR's `Observation`+`VisionPrescription` shape, given this is a significant, non-backward-compatible data-model change. **Not started.** This also now governs §5's five dashboards above, which are deliberately not FHIR-mapped yet either.
2. If pursued, sequence it similarly to past big builds in this project: a scoping conversation first (how much of this to adopt now vs. defer), then a background-agent build with migration + verification, then a living-spec update. **Not started.**
3. ~~Consider starting narrow: e.g., just add explicit habitual/manifest/cycloplegic refraction types (a small, high-value slice) before attempting full FHIR/DICOM/terminology alignment.~~ **Done, v2.8** — see the baseline spec's §12.6a.
4. This is a genuinely large scope (FHIR resource modeling, DICOM listener service, a terminology server/code-set integration) — likely multiple future sessions' worth of work, not a single round. Item 3 above was the first such slice; items 1-2 and the DICOM/terminology work remain fully open.
5. ~~Pick one of §5's five dashboards to actually scope and build (most likely §5.1's Refractive Assessment & Plan fields, since it extends the `Refraction`/`Prescription` work already shipped in v2.8 rather than introducing a wholly new table).~~ **Done, v2.10** — see §5.1.
6. **New, v2.9:** §5.6's e-prescribing/lab-integration/inventory material remains target-state only; revisit only once real vendor relationships or credentials exist to integrate against. **Not started.**
7. ~~Build a second dashboard (§5.2's Anterior Segment / Dry Eye), and, since that's the first dashboard to join Refractive Assessment on the same exam form, design how a clinician picks which Assessment & Plan section(s) apply to a given visit.~~ **Done, v2.11** — see §5.2 and the baseline spec's §12.5b (Visit Focus navigation model). §5.3–§5.5 (Glaucoma, Binocular Vision, Pre/Post-Op) remain undone; each adds one more Visit Focus chip by the same pattern, no changes to the toggle mechanism itself.
8. ~~The free-text `EyeExam.assessment`/`plan` fields (§12.5) have no connection to any of the structured Assessment fields built in §5.1/§5.2 — a clinician has to separately re-type in prose what they already selected as chips/dropdowns. The source document's dropped "Auto-Generated Clinical Note Output Summary" concept (see §5.1's correction note above) addresses exactly this: auto-populate `assessment`/`plan` with a draft narrative synthesized from the structured fields, left editable rather than locked.~~ **Done, v2.12** — see the baseline spec's §12.5c. Two further diagnosis-driven refinements to this composer (ICD-10 auto-suggestion, diagnosis-driven recall interval) are recorded as candidates in §7 below, from a later reviewed document.
9. **New, v2.13:** §6's billing/claims/EDI material remains target-state only, same treatment as §5.6 — revisit only with a real clearinghouse/payer relationship and compliance review, not attempted here. **Not started.**
10. ~~§7's two compatible ideas (ICD-10 auto-suggestion, diagnosis-driven recall interval) are candidates for a future narrow slice extending the existing v2.12 composer.~~ **Done, v2.15** — see the baseline spec's §12.5c.
11. ~~Build the third dashboard (§5.3's Posterior Segment / Glaucoma Tracking), including the longitudinal IOP trend view it wants beyond the other dashboards' single-visit-snapshot shape.~~ **Done, v2.16** — see §5.3 and the baseline spec's §12.5e. §5.4-§5.5 (Binocular Vision, Pre/Post-Op) remain undone; each adds one more Visit Focus chip by the same pattern.

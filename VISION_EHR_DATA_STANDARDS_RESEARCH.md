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

### 5.1 Comprehensive Refractive & Exam dashboard

Extends the existing `EyeExam`/`Refraction`/`Prescription` tables (§12.5–§12.7 of the baseline spec) rather than replacing them — the source document's "Assessment" fields are new, but its "Plan" (spectacle Rx) fields mostly already exist on `Prescription`.

| Field | Translated type | Notes |
| --- | --- | --- |
| Primary Refractive Dx | String (multi-value, comma-delimited or a child table) | Myopia / Hyperopia / Astigmatism / Presbyopia / Anisometropia / Emmetropia |
| Laterality | String | `OD` / `OS` / `OU` — matches this app's existing per-eye field-pair convention rather than a single laterality flag |
| Stability | String | Stable / Progressing / Improving |
| Secondary Findings | String (multi-value) | Amblyopia, Strabismus history, Cataract suspect, Suspect Glaucoma |
| Diagnosis code | Existing `EyeExam.diagnosis_codes` free-text field already covers this; an ICD-10 code table/auto-populate is future terminology-server work (§4.4), not new here |
| Lens Type *(new — not on `Prescription` today)* | String | Single Vision / Bifocal / Trifocal / Progressive / Office-Computer |
| Lens Material *(new)* | String | CR-39 / Polycarbonate / Trivex / Hi-Index 1.67 / Hi-Index 1.74 |
| Lens Treatments *(new)* | String (multi-value) | Anti-Reflective Coating, Blue Light Filter, Transitions/Photochromic, Polarized |
| Recall Interval *(new)* | String | 3 Months / 6 Months / 1 Year / 2 Years |
| Patient Education Tags *(new)* | String (multi-value) | 20-20-20 Rule, UV Protection, Contact Lens hygiene |

### 5.2 Anterior Segment & Ocular Surface Disease (Dry Eye) dashboard

A new encounter-scoped table, e.g. `AnteriorSegmentAssessment` (`patient_id`, `exam_id` FKs), following this app's existing pattern of `Refraction`/`AppointmentAuditEvent`-style child tables rather than widening `EyeExam` itself.

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

### 5.3 Posterior Segment & Glaucoma Tracking dashboard

A new longitudinal table, e.g. `GlaucomaTracking`, indexed on `(patient_id, created_at)` for trend charts — the source document's own indexing intent, achievable with a normal SQLAlchemy index rather than its Postgres-specific DDL.

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

## Next steps (partially started — see status notes)

1. Decide whether/when to align the `EyeExam`/`Refraction`/`Prescription` schema toward FHIR's `Observation`+`VisionPrescription` shape, given this is a significant, non-backward-compatible data-model change. **Not started.** This also now governs §5's five dashboards above, which are deliberately not FHIR-mapped yet either.
2. If pursued, sequence it similarly to past big builds in this project: a scoping conversation first (how much of this to adopt now vs. defer), then a background-agent build with migration + verification, then a living-spec update. **Not started.**
3. ~~Consider starting narrow: e.g., just add explicit habitual/manifest/cycloplegic refraction types (a small, high-value slice) before attempting full FHIR/DICOM/terminology alignment.~~ **Done, v2.8** — see the baseline spec's §12.6a.
4. This is a genuinely large scope (FHIR resource modeling, DICOM listener service, a terminology server/code-set integration) — likely multiple future sessions' worth of work, not a single round. Item 3 above was the first such slice; items 1-2 and the DICOM/terminology work remain fully open.
5. **New, v2.9:** pick one of §5's five dashboards to actually scope and build (most likely §5.1's Refractive Assessment & Plan fields, since it extends the `Refraction`/`Prescription` work already shipped in v2.8 rather than introducing a wholly new table). **Not started** — documentation only this round.
6. **New, v2.9:** §5.6's e-prescribing/lab-integration/inventory material remains target-state only; revisit only once real vendor relationships or credentials exist to integrate against. **Not started.**

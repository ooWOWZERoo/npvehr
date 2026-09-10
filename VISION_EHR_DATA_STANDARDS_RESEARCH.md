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

## Next steps (partially started — see status notes)

1. Decide whether/when to align the `EyeExam`/`Refraction`/`Prescription` schema toward FHIR's `Observation`+`VisionPrescription` shape, given this is a significant, non-backward-compatible data-model change. **Not started.**
2. If pursued, sequence it similarly to past big builds in this project: a scoping conversation first (how much of this to adopt now vs. defer), then a background-agent build with migration + verification, then a living-spec update. **Not started.**
3. ~~Consider starting narrow: e.g., just add explicit habitual/manifest/cycloplegic refraction types (a small, high-value slice) before attempting full FHIR/DICOM/terminology alignment.~~ **Done, v2.8** — see the baseline spec's §12.6a.
4. This is a genuinely large scope (FHIR resource modeling, DICOM listener service, a terminology server/code-set integration) — likely multiple future sessions' worth of work, not a single round. Item 3 above was the first such slice; items 1-2 and the DICOM/terminology work remain fully open.

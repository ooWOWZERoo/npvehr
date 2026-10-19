# Patient Medication List — Design (draft for decision)

**Status:** design only, nothing built. **Why:** the safety warnings (spec §112) can only match typed exam text today, so a rule like "blood thinner + dilating drops" can't know what a patient actually takes. A structured, reviewed medication list is the proper source. **Posture:** same as the rest of the app — staff-facing decision support, append-only new tables, curated lookups that stay silent until a clinician signs them off, advisory banners (never blocking), audited. Not for real patient data (spec banner).

## 1. What exists today
- `patients.allergies` is a free-text column; no medication data anywhere. The Visit Note reference the practice supplied lists medications, allergies and an alerts checklist as separate sections, so this matches how the practice already thinks.
- `patient_safety_flags` holds manual yes/no answers per patient ("Takes a blood thinner"); `safety.evaluate()` is the single place a rule's firing is decided (exam banner, exam detail, sign step all call it).
- E-prescribing / RxNorm is documented as target-state only (backlog). **This design does not add it** — no outbound integration, no drug database licence.

## 2. Proposed tables (all new; no existing table changes)
| Table | Purpose |
| --- | --- |
| `patient_medications` | One row per medication a patient takes or took: name as entered, strength/dose, route, frequency, **eye (OD/OS/OU or none)** for ocular meds, indication, `status` (active / stopped), start and stop dates, source (patient-reported / entered by staff / imported), recorded by + when, optional free note. Stopping sets status + stop date; rows are never deleted (history stays). |
| `medication_list_reviews` | One row each time a clinician confirms the list ("reviewed, no changes", "reviewed, updated", or **"reviewed: patient takes no medications"**), who/when, optional exam link, and a count snapshot. This is what makes an *empty* list mean something: no review row = unknown, never "none". |
| `medication_classes` | Practice-defined drug classes the safety rules care about (e.g. "Anticoagulant / antiplatelet", "Systemic anticholinergic — narrow-angle caution"). Label, description, active. Seeded **empty** (clinicians define them), like safety rules. |
| `medication_class_terms` | Generic and brand names that belong to a class (`warfarin`, `coumadin`, …), matched case-insensitively as whole words against the medication name. Each term carries **source citation + reviewed + reviewer/time**; an unreviewed term never classifies anything (same sign-off shape as ROS rules; editing withdraws sign-off). |
| `safety_flag_class_links` | Links a safety flag type to one or more classes, so "Takes a blood thinner" can be *derived* from the list. |

Audit: field-change audit for every add/edit/stop (existing mechanism), plus the review rows above.

## 3. How it feeds the safety warnings
- A flag is **derived "yes"** when the patient has an active medication whose name matches a *reviewed* term in a class linked to that flag. `safety.evaluate()` gets one additive step: effective status = manual answer if one exists and no derived "yes" applies, otherwise derived yes. Reviewed-rule gating, keyword matching and acknowledge-to-sign are unchanged.
- **Conflicts surface, not hide:** if staff recorded flag = "no" but an active med now derives "yes", the derived yes wins for firing and the patient's safety card shows "Medication list says otherwise: warfarin" so the stale manual answer gets fixed. (Safer default for a warning system; open question 2.)
- Unmatched medications are listed as **"not classified"** with a count, so the practice can see coverage gaps instead of assuming silence means safe. Matching is plain text (no brand/generic knowledge beyond the terms the clinicians enter) — said plainly on screen.

## 4. Screens
1. **Medications tab** in the patient workspace (new, beside Problem List): active list with eye/dose/frequency, "Add medication", stop/edit, history of stopped meds, "Mark list reviewed" (three outcomes above), "last reviewed by X on date" and an ambient banner if never reviewed or older than a configurable window (default 12 months). Same card/table styling as the other tabs.
2. **Exam form**: a compact read-only medications card in the intake step with "Review / update" and a one-click "Reviewed today — no changes" that writes a review row linked to the exam. Advisory only; the exam saves and signs regardless (an unreviewed list shows an info banner).
3. **Admin → Medication classes** (System/Practice Administrator; reviewer role signs terms off), cloned from `/admin/safety`: classes, terms with citation, reviewed toggle, flag links. Bulk term import can reuse the ROS import pattern later.
4. **Patient safety card** gains the "from medication list" label and the conflict line.

Roles: edit = `EXAM_EDIT` roles (clinical staff); class/term admin + review = the safety-rule admin/review sets; auditors view. Permission constants added to `ehr/auth/permissions.py` as usual.

## 5. Phasing (each phase its own PR, full suite, docs)
1. **List + tab + audit + review events** (no safety coupling). Useful on its own.
2. **Classes, terms, sign-off, flag links, derived flags in `evaluate()`** + admin screen. Nothing fires until clinicians load and sign off terms.
3. **Exam-form card + banners** (never-reviewed / stale).
4. **Later, only if wanted:** CSV import of medication lists (import-batch pattern, undo), portal view/"report a change", allergy structuring, drug–drug interaction content (needs a licensed source and clinical governance — explicitly out of scope).

## 6. Risks and mitigations
- **Stale list = false reassurance** → review rows, "last reviewed" everywhere, stale banner, "unknown" never shown as "none".
- **Free-text names defeat matching** → curated terms + visible "not classified" bucket; optional pick-list from terms for common drugs to cut typos (phase 2).
- **Over-warning** → warnings fire only from clinician-reviewed rules; acknowledgement is per exam, as today.
- **Scope creep into e-prescribing/interaction checking** → stated out of scope; no drug database is bundled or required.
- **PHI** → medication data is as sensitive as the rest; same test-data-only posture, no import or outbound path in phases 1–3.

## 7. Decisions (from the practice)
1. **Build phases 1 and 2 together.**
2. **Derived "yes" wins** over a manual "no" (conflict shown on the safety card).
3. Stale-review window: **12 months** (not answered; default, easy to change).
4. **Structured allergies ride along in phase 1** (`patient_allergies`: allergen, reaction, severity, status, same audit/review shape; the free-text `patients.allergies` stays and is shown read-only on the tab until staff migrate it).

## 8. (Resolved) open questions

1. Start with phase 1 only, or phases 1–2 together (so safety rules can use the list sooner)?
2. Conflict rule in §3 — derived "yes" wins over a manual "no" (recommended), or manual always wins?
3. Stale-review window default: 12 months, or practice-specific (e.g. every visit)?
4. Should structured **allergies** ride along in phase 1 (same shape: allergen, reaction, severity, status), or stay free-text for now?

## 9. Guided medication intake (v2.99) -- catalog, REST API, and the RxNorm decision

**What it is.** The Medications tab's "Add a medication" form is now a four-field chain: Medication (autocomplete) -> Dosage -> Frequency, plus Eye. A technician picks from a catalog or uses **Add Custom** to type anything. The older free-text columns remain the record of what was entered, so history never depends on the catalog.

**Tables (all new, nothing existing altered except nullable columns on `patient_medications`):** `drug_catalog` (generic/brand name, usual route, `rxcui` for RxNorm rows, `source` rxnorm|manual), `drug_dosages` (many per drug), `drug_frequencies` (the practice's pick list grouped ophthalmic/oral/topical/any, seeded with plain dosing wording). `patient_medications` gains nullable `drug_id`, `dosage_id`, `frequency_id` (real FKs, `ON DELETE SET NULL`), `drug_rxcui`, and `is_custom`; custom entries leave the keys NULL. Migrations 060 (columns, after `create_all` so the referenced tables exist) and 061 (frequency seed) are idempotent; 060 has no down-migration (SQLite cannot drop FK columns and old code ignores them).

**REST contract** (`ehr/routes/medication_intake_api.py`; the UI calls only these; JSON; errors are `{"detail": ...}`; 401 signed out, 403 role or missing `X-CSRF-Token`, 404 unknown drug or patient outside the caller's access, 422 validation):

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/drugs/search?q=` | 3+ characters (enforced server-side); prefix matches first; max 15; LIKE wildcards are literal |
| `GET /api/v1/drugs/{id}/dosages` | that drug's dosages, each with `is_oral` |
| `GET /api/v1/drugs/{id}/frequencies[?dosage_id=]` | the pick list, the drug's route group (plus "any") first, flagged `preferred`, nothing dropped |
| `POST /api/v1/patients/{id}/medications` | add a catalog pick or custom entry; clinical-staff roles only; audited |

**Rules enforced on the server:** a dosage must belong to the chosen drug; an **oral drug always records eye = PO and route = oral** (whatever the client sent); PO is refused for ophthalmic/topical drugs; a custom entry may use any eye value and needs only a name and eye.

**UI behaviour:** search debounced 300 ms, only from 3 characters, stale responses ignored; Dosage unlocks after a drug, Frequency after a dosage, Eye once a name exists; **Add Custom** is the last row of the list at all times and turns Dosage/Frequency into free text; a catalog drug with no dosages on file offers "Enter dosage manually".

**Catalog source -- decision.** The practice chose a **public drug source (NLM RxNorm / RxNav)** over a curated starter list. This is the app's first outbound integration, so, deliberately: only the typed drug-name text would ever be sent (never patient data), results are copied into the local tables (the intake itself only ever reads our database), and a lookup failure degrades to the local catalog plus Add Custom. **Not built yet:** the RxNorm loader. RxNav was unreachable from the development sandbox (network policy), and the loader must be written and checked against the live API rather than guessed. Until it runs, the catalog is empty (the app ships no drug data) and Add Custom is the working path.


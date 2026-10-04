# Proposal: Offering a Routine-to-Medical Visit Switch from Exam Findings

**Status:** proposal for review by the practice's coder / billing compliance lead. **Nothing is built.** No code is written until the decisions in section 7 are signed off.
**Why a coder first:** this touches which insurance a visit is billed to and what the patient is told they owe. The app should not guess; it should only help staff notice and act, with the coder's rules written down.

## 1. What this is, and is not
- **Is:** an advisory prompt on the exam page ("this visit was booked as routine vision, but the findings look medical") with a button that switches the visit's *flow* to Medical, after a required reason, recorded in an audit table.
- **Is not:** a claim, a submission, an automatic switch, a coding change, or an E/M/diagnosis suggestion engine. The app has no billing or claims infrastructure and none is added. A banner is decision support only, never blocking, per the app's standing conventions.

## 2. How the app models the two flows today (from the code)
- Each appointment has `visit_flow` = `vision` | `medical` and `visit_flow_source` = `automatic` | `manual_override`. At check-in it is **suggested** from insurance on file (an active medical plan suggests `medical`, otherwise `vision`), and front desk can change it on the appointment page. Nothing else changes it.
- The exam page's **Billing Preview** (read-only, "not a submitted claim") renders the two flows differently:
  - **Vision:** one vision-plan claim: refraction 92015, exam code (92004 new / 92014 established), testing codes.
  - **Medical:** Claim 1 to medical insurance: E/M (99212-99214 as suggested/confirmed), exam code, testing codes, diagnoses; Claim 2 to the patient (with an ABN label): refraction 92015 as non-covered.
- Related, already built: exam-type triage from the chief complaint (Routine Vision / Medical / Emergent...), an MDM-based E/M suggestion, curated NCCI advisories, ROS catalog findings with ICD-10/CPT rules that are **silent until a reviewer signs them off**, and the "checkbox + required reason" override pattern used for billing overrides.
- **Gap:** a patient booked as routine can present a medical problem during the exam; the flow stays `vision` unless someone remembers to change it on the appointment page, and the preview then shows the wrong shape.

## 3. Proposed trigger (the coder chooses; recommendation marked)
The prompt appears on an exam **only when the linked appointment's flow is `vision`** and at least one of these evidence tiers applies:

| Option | Evidence | Notes |
| --- | --- | --- |
| A (narrowest) | A ticked ROS finding whose rule is **reviewed** and whose suggested ICD-10 is a medical (non-refractive) diagnosis | Silent until ROS stage 1 sign-off is done. ROS positives are a *symptom hint*, not medical necessity by themselves. |
| **B (recommended)** | A **or** the clinician has entered a diagnosis code on the exam that the coder classifies as *medical* (section 5 table) | The clinician's own diagnosis is better evidence than a ticked history item. |
| C (broadest) | B **or** the chief-complaint triage lands on a medical category | Triage is keyword-based; likely to over-prompt. Not recommended. |

If the exam has no linked appointment (walk-in), no prompt appears (there is no flow to switch), consistent with the billing preview.

## 4. What the switch would do
1. **Banner** on the exam page: states which evidence fired (e.g. "Dx H40.1131 entered; ROS finding 'History of diabetes' with a reviewed rule") and links to the Billing Preview.
2. **Button "Switch this visit to Medical"** (roles: those who may edit the appointment's flow today, plus the signing provider; coder to confirm) opens a small form: confirm-checkbox and a **required reason** (the app's override shape: `switch_override` + `switch_override_reason`).
3. On confirm: sets the appointment's `visit_flow = 'medical'`, `visit_flow_source = 'switched_at_exam'`, writes an **audit row** (new small table `visit_flow_switch_events`: appointment, exam, from, to, evidence summary, reason, user, time). The Billing Preview then renders the medical shape. Reversible by the existing appointment-page control, which is also audited.
4. A reminder line on the banner: "Tell the patient their billing is changing before checkout (refraction becomes patient responsibility)" with a checkbox "Patient informed" recorded in the same audit row. *(Coder to confirm wording and whether a checkbox is wanted at all.)*
5. **Dismiss** ("keep as routine") with an optional reason, so the prompt doesn't nag; recorded likewise.

**Not changed by a switch:** the diagnosis codes, the E/M or exam code selections, charges already added to the patient ledger at exam save (open question 7.7), the NCCI advisories (they re-evaluate on the codes shown), and any claim (none exists).
**Not proposed:** the reverse direction (medical to routine), or switching without a person confirming.

## 5. Proposed starting "medical vs routine" diagnosis classification (coder to edit)
A small curated table, editable by administrators, **inactive until a coder signs it off with a citation** (the same reviewed-rule pattern as the ROS and safety rules). Starting proposal, for correction:

| Class | Proposed codes | Meaning for the prompt |
| --- | --- | --- |
| Routine / refractive | H52.* (refractive error, presbyopia, astigmatism), Z01.00, Z01.01 (encounter for eye exam) | Never prompts |
| Medical | Any other H00-H59 code (glaucoma, cataract, dry eye, retinal disease, conjunctivitis...), E10/E11 with an ophthalmic complication, other systemic codes with an ocular manifestation | Counts as medical evidence |
| Needs review | Symptom codes (e.g. H53.* visual disturbances, H57.1x eye pain) | Coder decides which side |

## 6. Technical sketch (once approved)
- Two new tables: `visit_flow_switch_events` (audit) and `diagnosis_visit_class` (the curated classification with citation, reviewed flag, reviewer, time). Existing tables unchanged; `visit_flow_source` just gains a new string value.
- Reuses: `cpt_mapper.suggest_visit_flow`, the appointment flow-update logic, the override pattern, `field_audit`, the ambient `.alert-info` banner (never a modal), and the ROS reviewed-rule data for option A.
- Permissions as named constants in `ehr/auth/permissions.py`. Tests: unit tests for the trigger rule and classification; end-to-end for banner, reason-required, audit row, preview change, dismiss. Documented in the spec and backlog like every feature.
- Size: one PR, comparable to the PCP letter; the classification screen is the bulk of the work.

## 7. Questions for the coder (please answer or amend)
1. **Is switching flow after the visit has started acceptable** under the payers you bill (medical insurance vs vision plan), and what must be documented to support it?
2. **Which trigger option** (A, B or C) do you want, and should it stay silent until you sign off the classification table?
3. **Classification table:** please correct section 5. Which symptom codes count as medical?
4. **E/M plus an exam code on the same medical claim:** the current Billing Preview for the medical flow lists an E/M code *and* 92004/92014 together on Claim 1. Is that how you want a medical visit represented, or should the medical preview show only one of them? (This exists today and is independent of this proposal, but a switch will surface it more often.)
5. **Patient notice:** is a "patient informed" checkbox appropriate, what exact wording, and is the ABN label in the preview correct for your payers?
6. **Who may confirm a switch:** signing provider only, front desk and provider, or coder only?
7. **Charges already on the ledger:** exam save auto-adds the exam code and the confirmed E/M code as ledger charges. After a switch, should anything on the ledger change (for example refraction moving to patient responsibility), or stay as is for billing staff to adjust?
8. **Dismissals:** should "keep as routine" require a reason?
9. **Documentation of medical necessity** (separate chief complaint, history, assessment for the medical portion; any modifier question when routine and medical services share a visit): what, if anything, should the app prompt for? (The proposal prompts for none; it only switches the flow.)

## 8. Sign-off
Reviewed by: ________________  Date: ________  Option chosen: A / B / C  Answers to section 7 attached: yes / no

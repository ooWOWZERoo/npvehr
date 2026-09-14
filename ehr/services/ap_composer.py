"""Assessment & Plan (A&P) composer: the core, UI-independent logic behind
the New Exam form's click-to-autofill Assessment/Plan fields (v2.26).

This module is the reference implementation of three things the live form's
JavaScript composer (`ehr/templates/exams/form.html`) mirrors line-for-line
so both stay in lockstep:

1. A curated ICD-10-CM lookup + validator (`ICD10_TABLE`, `resolve_icd10`) --
   the same engineering tradeoff this app has used since v2.15 (see
   VISION_EHR_DATA_STANDARDS_RESEARCH.md 4.4/7.2): a small table of codes for
   exactly the diagnoses this app's dashboards structurally capture, checked
   against the current ICD-10-CM tabular list at write time, not a real
   terminology-server integration.
2. Two output styles -- NARRATIVE (full sentences, the style this app has
   used since v2.12) and ABBREVIATED (terse bulleted clinical fragments,
   e.g. "- H11.031 - Pterygium OD - Mild") -- built from the same
   structured `Finding`/`PlanItem` inputs so neither style can drift out of
   sync with the other.
3. A manual-edit-preserving merge function (`merge_manual_edits`) so
   re-running the composer after a clinician has typed directly into the
   Assessment/Plan textarea never silently discards what they wrote --
   whether they appended a note, inserted one mid-note, or edited an
   auto-generated line itself (which safely freezes future auto-updates to
   that field, exactly like the app's existing simpler "edited" flags on
   diagnosis_codes/follow_up_weeks).

Deliberately UI-independent: nothing here reads HTML, a request, or a
database session. `ehr/routes/exams.py` and `exams/form.html`'s own script
own the presentation layer; this module owns only the text/validation logic
that layer calls into (or, on the client, mirrors).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enums / small value types
# ---------------------------------------------------------------------------

class Laterality(str, Enum):
    OD = "OD"
    OS = "OS"
    OU = "OU"
    UNSPECIFIED = ""  # no eye-specific exam data exists for this finding


class GlaucomaStage(str, Enum):
    """Matches the ICD-10-CM 7th-character values for staged glaucoma codes
    (H40.11-, H40.12-, ...): 0 unspecified, 1 mild, 2 moderate, 3 severe,
    4 indeterminate."""
    UNSPECIFIED = "Unspecified"
    MILD = "Mild"
    MODERATE = "Moderate"
    SEVERE = "Severe"
    INDETERMINATE = "Indeterminate"


_STAGE_DIGIT = {
    GlaucomaStage.UNSPECIFIED: "0",
    GlaucomaStage.MILD: "1",
    GlaucomaStage.MODERATE: "2",
    GlaucomaStage.SEVERE: "3",
    GlaucomaStage.INDETERMINATE: "4",
}
_LATERALITY_DIGIT = {Laterality.OD: "1", Laterality.OS: "2", Laterality.OU: "3", Laterality.UNSPECIFIED: "9"}


class NoteStyle(str, Enum):
    NARRATIVE = "narrative"
    ABBREVIATED = "abbreviated"


class IssueSeverity(str, Enum):
    ERROR = "error"      # never silently emit this code -- the caller must resolve it
    WARNING = "warning"  # emitted, but flagged for clinician review


# ---------------------------------------------------------------------------
# ICD-10 lookup + validation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Icd10Rule:
    """One diagnosis's coding rule. `codes` is keyed by Laterality for a
    plain diagnosis; `staged_codes` (glaucoma-family diagnoses only) is
    keyed by (Laterality, GlaucomaStage) and takes precedence over `codes`
    when `requires_staging` is True."""
    description: str
    requires_laterality: bool
    codes: dict = field(default_factory=dict)          # Laterality -> code
    requires_staging: bool = False
    staged_codes: dict = field(default_factory=dict)   # (Laterality, GlaucomaStage) -> code


def _staged_poag_codes(base: str) -> dict:
    """Builds the 7-character H40.11-style code grid: base + laterality
    digit + stage digit, for every (Laterality, GlaucomaStage) pair."""
    out = {}
    for lat, lat_digit in _LATERALITY_DIGIT.items():
        for stage, stage_digit in _STAGE_DIGIT.items():
            out[(lat, stage)] = f"{base}{lat_digit}{stage_digit}"
    return out


# Every code below was checked against the current (FY2026) ICD-10-CM
# tabular list when added. Kept intentionally narrow -- exactly the
# diagnoses this app's Visit Focus dashboards structurally capture.
ICD10_TABLE: dict[str, Icd10Rule] = {
    # Refractive Assessment
    "Myopia": Icd10Rule("Myopia", True, {
        Laterality.OD: "H52.11", Laterality.OS: "H52.12", Laterality.OU: "H52.13", Laterality.UNSPECIFIED: "H52.10"}),
    "Hyperopia": Icd10Rule("Hyperopia", True, {
        Laterality.OD: "H52.01", Laterality.OS: "H52.02", Laterality.OU: "H52.03", Laterality.UNSPECIFIED: "H52.00"}),
    "Astigmatism": Icd10Rule("Astigmatism", True, {
        Laterality.OD: "H52.201", Laterality.OS: "H52.202", Laterality.OU: "H52.203", Laterality.UNSPECIFIED: "H52.209"}),
    "Presbyopia": Icd10Rule("Presbyopia", False, {  # no laterality split in ICD-10 itself
        Laterality.OD: "H52.4", Laterality.OS: "H52.4", Laterality.OU: "H52.4", Laterality.UNSPECIFIED: "H52.4"}),
    "Anisometropia": Icd10Rule("Anisometropia", False, {
        Laterality.OD: "H52.31", Laterality.OS: "H52.31", Laterality.OU: "H52.31", Laterality.UNSPECIFIED: "H52.31"}),

    # Anterior Segment
    "Pterygium": Icd10Rule("Pterygium", True, {
        Laterality.OD: "H11.031", Laterality.OS: "H11.032", Laterality.OU: "H11.033"}),
    "Pinguecula": Icd10Rule("Pinguecula", True, {
        Laterality.OD: "H11.151", Laterality.OS: "H11.152", Laterality.OU: "H11.153"}),
    "Nuclear Sclerosis Cataract": Icd10Rule("Age-related nuclear sclerotic cataract", True, {
        Laterality.OD: "H25.11", Laterality.OS: "H25.12", Laterality.OU: "H25.13"}),
    "Cortical Cataract": Icd10Rule("Age-related cortical cataract", True, {
        Laterality.OD: "H25.011", Laterality.OS: "H25.012", Laterality.OU: "H25.013"}),
    "Posterior Subcapsular Cataract": Icd10Rule("Age-related posterior subcapsular cataract", True, {
        Laterality.OD: "H25.041", Laterality.OS: "H25.042", Laterality.OU: "H25.043"}),
    "Combined Cataract": Icd10Rule("Combined forms of age-related cataract", True, {
        Laterality.OD: "H25.811", Laterality.OS: "H25.812", Laterality.OU: "H25.813"}),

    # Dry Eye
    "Dry Eye Syndrome": Icd10Rule("Dry eye syndrome", True, {
        Laterality.OD: "H04.121", Laterality.OS: "H04.122", Laterality.OU: "H04.123"}),

    # Posterior Segment / Glaucoma -- the one diagnosis requiring 7th-character staging
    "Primary Open-Angle Glaucoma": Icd10Rule(
        "Primary open-angle glaucoma", True, requires_staging=True,
        staged_codes=_staged_poag_codes("H40.11")),

    # Binocular Vision
    "Convergence Insufficiency": Icd10Rule("Convergence insufficiency", False, {
        Laterality.OD: "H51.11", Laterality.OS: "H51.11", Laterality.OU: "H51.11", Laterality.UNSPECIFIED: "H51.11"}),

    # Pre-/Post-Op Co-Management (aftercare status, not a new diagnosis)
    "Cataract Extraction Aftercare": Icd10Rule("Cataract extraction status", True, {
        Laterality.OD: "Z98.41", Laterality.OS: "Z98.42"}),  # no bilateral code exists for Z98.4
}


@dataclass
class ValidationIssue:
    severity: IssueSeverity
    message: str


@dataclass
class Icd10Resolution:
    code: Optional[str]
    issues: list[ValidationIssue] = field(default_factory=list)


def resolve_icd10(
    diagnosis_key: str,
    laterality: Laterality = Laterality.UNSPECIFIED,
    stage: GlaucomaStage = GlaucomaStage.UNSPECIFIED,
) -> Icd10Resolution:
    """Looks up the ICD-10 code for a diagnosis, enforcing:

    - **Laterality specificity**: eye-care codes require a specific 6th (or,
      for staged codes, an earlier) character for OD/OS/OU. If the exam
      actually recorded a specific eye (`laterality` is OD/OS/OU) but the
      diagnosis's code table has no laterality-specific entry to honor that,
      or the caller passed UNSPECIFIED anyway, this is flagged as an ERROR
      -- an unspecified code must never be silently chosen when the exam
      already told us which eye. Passing UNSPECIFIED is only ever fine when
      the exam itself never captured a specific eye for this finding.
    - **Glaucoma staging**: a `requires_staging` diagnosis with `stage`
      UNSPECIFIED gets a WARNING (the code is still resolved, using stage
      digit '0', since ICD-10-CM does define an unspecified-stage code --
      but billing/documentation quality strongly prefers a real stage when
      the exam has one available; see `GlaucomaStage`).
    """
    rule = ICD10_TABLE.get(diagnosis_key)
    if rule is None:
        return Icd10Resolution(None, [ValidationIssue(
            IssueSeverity.ERROR, f"Unknown diagnosis '{diagnosis_key}' -- not in the curated ICD-10 table.")])

    issues: list[ValidationIssue] = []

    if rule.requires_staging:
        if stage == GlaucomaStage.UNSPECIFIED:
            issues.append(ValidationIssue(
                IssueSeverity.WARNING,
                f"{rule.description}: no glaucoma stage selected -- coding with the 7th-character "
                "'unspecified' digit (0). Select a stage (Mild/Moderate/Severe/Indeterminate) if the "
                "exam supports one; unspecified stage is a documentation-quality flag, not billable "
                "ambiguity to leave unresolved when avoidable."))
        code = rule.staged_codes.get((laterality, stage)) or rule.staged_codes.get((Laterality.UNSPECIFIED, stage))
        if laterality == Laterality.UNSPECIFIED and rule.requires_laterality:
            issues.append(ValidationIssue(
                IssueSeverity.WARNING,
                f"{rule.description}: no laterality selected -- coding with the unspecified-eye digit (9)."))
        return Icd10Resolution(code, issues)

    if rule.requires_laterality and laterality == Laterality.UNSPECIFIED and Laterality.UNSPECIFIED not in rule.codes:
        # The rule has no unspecified-eye entry at all -- the exam MUST supply
        # a real eye for this diagnosis to be codeable. Hard error, never guess.
        issues.append(ValidationIssue(
            IssueSeverity.ERROR,
            f"{rule.description}: laterality is required and no unspecified-eye code exists for this "
            "diagnosis -- select OD, OS, or OU before this can be coded."))
        return Icd10Resolution(None, issues)

    code = rule.codes.get(laterality)
    if code is None:
        issues.append(ValidationIssue(
            IssueSeverity.ERROR, f"{rule.description}: no code defined for laterality '{laterality.value}'."))
    return Icd10Resolution(code, issues)


def reject_unspecified_when_known(laterality_from_exam: Laterality, resolution: Icd10Resolution) -> Icd10Resolution:
    """Defensive guard matching the stated requirement verbatim: 'Never
    allow Unspecified laterality if laterality was specified in the exam
    checkboxes.' `resolve_icd10` already refuses to guess when the rule has
    no unspecified code to fall back to; this catches the remaining case --
    a rule that DOES define an unspecified-eye code (e.g. Myopia's H52.10)
    but the exam nonetheless recorded a specific eye -- and hard-fails
    instead of silently emitting the less-specific code."""
    if laterality_from_exam != Laterality.UNSPECIFIED and resolution.code:
        rule = None
        for r in ICD10_TABLE.values():
            if resolution.code in r.codes.values() or resolution.code in r.staged_codes.values():
                rule = r
                break
        unspecified_code = (rule.codes.get(Laterality.UNSPECIFIED) if rule else None)
        if unspecified_code and resolution.code == unspecified_code:
            return Icd10Resolution(None, resolution.issues + [ValidationIssue(
                IssueSeverity.ERROR,
                "Exam recorded a specific eye for this finding, but the resolved code is the "
                "unspecified-eye code -- refusing to emit it. This indicates a caller bug (laterality "
                "was dropped before calling resolve_icd10), not a valid clinical state.")])
    return resolution


# ---------------------------------------------------------------------------
# Structured inputs
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    """One clicked/selected assessment item -- a diagnosis with its
    laterality, severity/status text, and (glaucoma only) stage."""
    diagnosis_key: str          # must exist in ICD10_TABLE
    laterality: Laterality = Laterality.UNSPECIFIED
    severity_or_status: str = ""   # e.g. "Mild", "Stable", "Trace"
    stage: GlaucomaStage = GlaucomaStage.UNSPECIFIED


@dataclass
class MedOrder:
    drug: str
    concentration: str = ""
    frequency: str = ""         # e.g. "QHS", "BID", "TID", "QD"
    laterality: Laterality = Laterality.OU


@dataclass
class DiagnosticOrder:
    name: str


@dataclass
class FollowUp:
    timeframe: str               # e.g. "2 weeks", "RTC 6 months"


@dataclass
class ComposedNote:
    assessment: str
    plan: str
    issues: list[ValidationIssue] = field(default_factory=list)


# ---------------------------------------------------------------------------
# String builders
# ---------------------------------------------------------------------------

_NARRATIVE_DIAGNOSIS_TEXT = {
    "Pterygium": "Pterygium",
    "Pinguecula": "Pinguecula",
    "Nuclear Sclerosis Cataract": "Nuclear sclerotic cataract",
    "Cortical Cataract": "Cortical cataract",
    "Posterior Subcapsular Cataract": "Posterior subcapsular cataract",
    "Combined Cataract": "Combined-form cataract",
    "Dry Eye Syndrome": "Dry eye syndrome",
    "Primary Open-Angle Glaucoma": "Primary open-angle glaucoma",
    "Convergence Insufficiency": "Convergence insufficiency",
    "Cataract Extraction Aftercare": "Cataract extraction, aftercare status",
}


def _diagnosis_label(diagnosis_key: str) -> str:
    return _NARRATIVE_DIAGNOSIS_TEXT.get(diagnosis_key, diagnosis_key)


def build_assessment_line(finding: Finding, resolution: Icd10Resolution, style: NoteStyle) -> str:
    """Renders one Finding + its resolved code as either a terse bulleted
    fragment or a narrative sentence. Never emits filler ("The patient
    presents with...") in either style -- narrative style is still a
    fragment-per-line clinical note, just in prose rather than dashes."""
    label = _diagnosis_label(finding.diagnosis_key)
    code = resolution.code or "UNCODED"
    lat = finding.laterality.value or ""
    status_bits = []
    if finding.severity_or_status:
        status_bits.append(finding.severity_or_status)
    if finding.stage != GlaucomaStage.UNSPECIFIED:
        status_bits.append(f"{finding.stage.value.lower()} stage")
    status = ", ".join(status_bits)

    if style == NoteStyle.ABBREVIATED:
        parts = [f"{code} - {label}"]
        if lat:
            parts.append(lat)
        line = " ".join(parts)
        if status:
            line += f" - {status}"
        return f"- {line}"

    # NARRATIVE
    sentence = label
    if lat:
        sentence += f" {lat}"
    if status:
        sentence += f", {status}"
    sentence += f" ({code})."
    return f"- {sentence}"


def build_plan_lines(meds: list[MedOrder], tests: list[DiagnosticOrder], follow_up: Optional[FollowUp], style: NoteStyle) -> list[str]:
    lines: list[str] = []
    if style == NoteStyle.ABBREVIATED:
        for m in meds:
            bits = [b for b in [m.drug, m.concentration, m.frequency, m.laterality.value] if b]
            lines.append("- Meds: " + ", ".join(bits))
        for t in tests:
            lines.append(f"- Testing: {t.name}")
        if follow_up:
            lines.append(f"- RTC: {follow_up.timeframe}")
        return lines

    # NARRATIVE
    for m in meds:
        dose = " ".join(b for b in [m.drug, m.concentration] if b)
        tail = " ".join(b for b in [m.frequency, m.laterality.value] if b)
        lines.append(f"- Start {dose}{(' ' + tail) if tail else ''}.".replace("  ", " "))
    for t in tests:
        lines.append(f"- Order {t.name}.")
    if follow_up:
        lines.append(f"- Return to clinic in {follow_up.timeframe}.")
    return lines


def compose_note(
    findings: list[Finding],
    meds: Optional[list[MedOrder]] = None,
    tests: Optional[list[DiagnosticOrder]] = None,
    follow_up: Optional[FollowUp] = None,
    style: NoteStyle = NoteStyle.ABBREVIATED,
) -> ComposedNote:
    """Top-level entry point: resolves ICD-10 for every finding, builds both
    Assessment and Plan text in the requested style, and surfaces every
    validation issue collected along the way (callers decide whether an
    ERROR should block saving the exam -- this module never raises)."""
    meds = meds or []
    tests = tests or []
    all_issues: list[ValidationIssue] = []
    assessment_lines = []
    for finding in findings:
        resolution = resolve_icd10(finding.diagnosis_key, finding.laterality, finding.stage)
        resolution = reject_unspecified_when_known(finding.laterality, resolution)
        all_issues.extend(resolution.issues)
        assessment_lines.append(build_assessment_line(finding, resolution, style))
    plan_lines = build_plan_lines(meds, tests, follow_up, style)
    return ComposedNote(
        assessment="\n".join(assessment_lines),
        plan="\n".join(plan_lines),
        issues=all_issues,
    )


# ---------------------------------------------------------------------------
# Editability & merge-state logic
# ---------------------------------------------------------------------------

@dataclass
class MergeResult:
    text: str
    frozen: bool  # True once the field has diverged enough that we stop auto-updating it


def merge_manual_edits(previous_auto: str, current_value: str, new_auto: str) -> MergeResult:
    """Reconciles a freshly recomputed auto-generated block (`new_auto`)
    with whatever is currently in the field (`current_value`), which may
    contain manual clinician edits made since the last auto-update
    (`previous_auto` is what we last wrote there).

    Three cases, in order of how much of the field we're willing to touch:

    1. The field is untouched or empty -- just write the new auto block.
    2. `current_value` still contains `previous_auto` intact somewhere
       (prefix, suffix, or spliced in the middle) -- meaning every edit the
       clinician made was an *addition* around the auto content, never an
       edit *to* it. Splice the new auto block into that same position,
       preserving every manually added character exactly where it was
       typed. This is the common case the spec calls out: appending
       "- patient reports poor compliance due to cost" below the generated
       bullets survives every subsequent checkbox click.
    3. `previous_auto` is no longer found intact in `current_value` -- the
       clinician edited an auto-generated line itself (retyped a bullet,
       deleted one by hand, etc). We cannot safely guess which parts are
       now "theirs" vs. "ours", so we freeze: return `current_value`
       unchanged and report `frozen=True`. This mirrors the app's existing,
       simpler edited-flag fields (diagnosis_codes/follow_up_weeks) -- once
       a clinician's own edit lands inside generated text, auto-updates for
       that field stop until the note is regenerated from scratch (e.g. a
       fresh Save/New Exam), rather than ever silently overwriting a human
       edit to clinical documentation.
    """
    if current_value == "" or previous_auto == "":
        return MergeResult(new_auto, False)

    idx = current_value.find(previous_auto)
    if idx == -1:
        return MergeResult(current_value, True)

    before = current_value[:idx]
    after = current_value[idx + len(previous_auto):]
    return MergeResult(before + new_auto + after, False)

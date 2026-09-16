"""Unit tests for the core Assessment & Plan composer logic
(ehr/services/ap_composer.py) -- pure Python, no app/DB/browser needed.
Covers: ICD-10 resolution + validation rules (laterality specificity,
glaucoma 7th-character staging), both output styles, and the manual-edit-
preserving merge logic."""
import pytest

from ehr.services.ap_composer import (
    Finding, Laterality, GlaucomaStage, NoteStyle, IssueSeverity,
    MedOrder, DiagnosticOrder, FollowUp,
    resolve_icd10, reject_unspecified_when_known, compose_note, merge_manual_edits,
)


# ---------------------------------------------------------------------------
# ICD-10 resolution
# ---------------------------------------------------------------------------

def test_resolves_laterality_specific_code():
    res = resolve_icd10("Pterygium", Laterality.OD)
    assert res.code == "H11.031"
    assert res.issues == []


def test_pterygium_has_no_unspecified_fallback_and_errors_without_laterality():
    res = resolve_icd10("Pterygium", Laterality.UNSPECIFIED)
    assert res.code is None
    assert any(i.severity == IssueSeverity.ERROR for i in res.issues)


def test_myopia_allows_unspecified_but_flags_when_exam_knew_the_eye():
    # Myopia's table DOES have an unspecified-eye code (H52.10) -- fine on its own.
    res = resolve_icd10("Myopia", Laterality.UNSPECIFIED)
    assert res.code == "H52.10"
    assert res.issues == []

    # But if the exam actually recorded OD and something still resolved to
    # the unspecified code, the guard must refuse it (defensive: never allow
    # Unspecified when laterality was specified in the exam checkboxes).
    fake_unspecified_result = resolve_icd10("Myopia", Laterality.UNSPECIFIED)
    guarded = reject_unspecified_when_known(Laterality.OD, fake_unspecified_result)
    assert guarded.code is None
    assert any(i.severity == IssueSeverity.ERROR for i in guarded.issues)

    # The normal path (laterality correctly threaded through) is unaffected.
    normal = reject_unspecified_when_known(Laterality.OD, resolve_icd10("Myopia", Laterality.OD))
    assert normal.code == "H52.11"


def test_glaucoma_requires_7th_character_staging():
    res = resolve_icd10("Primary Open-Angle Glaucoma", Laterality.OU, GlaucomaStage.MODERATE)
    assert res.code == "H40.1132"  # base + OU digit(3) + moderate digit(2)
    assert res.issues == []

    unstaged = resolve_icd10("Primary Open-Angle Glaucoma", Laterality.OU, GlaucomaStage.UNSPECIFIED)
    assert unstaged.code == "H40.1130"  # still resolves (0 = unspecified stage is a real code)
    assert any(i.severity == IssueSeverity.WARNING and "stage" in i.message.lower() for i in unstaged.issues)


def test_glaucoma_all_laterality_stage_combinations_are_valid_eight_char_codes():
    for lat in Laterality:
        for stage in GlaucomaStage:
            res = resolve_icd10("Primary Open-Angle Glaucoma", lat, stage)
            assert res.code is not None
            assert res.code.startswith("H40.11")
            assert len(res.code) == 8  # 'H40.11' + laterality digit + stage digit


def test_unknown_diagnosis_errors():
    res = resolve_icd10("Made Up Diagnosis")
    assert res.code is None
    assert res.issues[0].severity == IssueSeverity.ERROR


# ---------------------------------------------------------------------------
# String builders / both styles
# ---------------------------------------------------------------------------

def test_abbreviated_style_is_bulleted_fragments_not_narrative():
    note = compose_note(
        findings=[Finding("Pterygium", Laterality.OD, "Mild")],
        style=NoteStyle.ABBREVIATED,
    )
    assert note.assessment == "- H11.031 - Pterygium OD - Mild"
    assert "presents with" not in note.assessment.lower()
    assert "the patient" not in note.assessment.lower()


def test_narrative_style_is_still_fragment_per_line_no_filler():
    note = compose_note(
        findings=[Finding("Pterygium", Laterality.OD, "Mild")],
        style=NoteStyle.NARRATIVE,
    )
    assert note.assessment == "- Pterygium OD, Mild (H11.031)."
    assert "presents with" not in note.assessment.lower()


def test_plan_lines_use_meds_testing_rtc_structure():
    note = compose_note(
        findings=[Finding("Primary Open-Angle Glaucoma", Laterality.OU, stage=GlaucomaStage.MODERATE)],
        meds=[MedOrder("Latanoprost", "0.005%", "QHS", Laterality.OU)],
        tests=[DiagnosticOrder("Humphrey VF 24-2")],
        follow_up=FollowUp("3 months"),
        style=NoteStyle.ABBREVIATED,
    )
    assert "- Meds: Latanoprost, 0.005%, QHS, OU" in note.plan
    assert "- Testing: Humphrey VF 24-2" in note.plan
    assert "- RTC: 3 months" in note.plan


def test_abbreviations_used_over_full_words():
    note = compose_note(
        findings=[],
        meds=[MedOrder("Timolol", "0.5%", "BID", Laterality.OU)],
        follow_up=FollowUp("2 weeks"),
        style=NoteStyle.ABBREVIATED,
    )
    assert "BID" in note.plan
    assert "OU" in note.plan


def test_multiple_findings_each_get_their_own_line():
    note = compose_note(
        findings=[
            Finding("Pterygium", Laterality.OD, "Mild"),
            Finding("Nuclear Sclerosis Cataract", Laterality.OS, "Trace"),
        ],
        style=NoteStyle.ABBREVIATED,
    )
    lines = note.assessment.split("\n")
    assert len(lines) == 2
    assert "H11.031" in lines[0]
    assert "H25.12" in lines[1]


def test_compose_note_surfaces_validation_issues_without_raising():
    note = compose_note(
        findings=[Finding("Pterygium", Laterality.UNSPECIFIED)],
        style=NoteStyle.ABBREVIATED,
    )
    assert any(i.severity == IssueSeverity.ERROR for i in note.issues)
    assert "UNCODED" in note.assessment  # still renders a line, just flags the problem


# ---------------------------------------------------------------------------
# Manual-edit-preserving merge
# ---------------------------------------------------------------------------

def test_merge_writes_fresh_when_field_was_empty():
    result = merge_manual_edits(previous_auto="", current_value="", new_auto="- A\n- B")
    assert result.text == "- A\n- B"
    assert result.frozen is False


def test_merge_preserves_manual_text_appended_after_auto_block():
    prev = "- A"
    current = "- A\n- patient reports poor compliance due to cost"
    new_auto = "- A\n- C"
    result = merge_manual_edits(prev, current, new_auto)
    assert result.text == "- A\n- C\n- patient reports poor compliance due to cost"
    assert result.frozen is False


def test_merge_preserves_manual_text_prepended_before_auto_block():
    prev = "- A"
    current = "Chief complaint: red eye x3 days.\n- A"
    new_auto = "- A\n- B"
    result = merge_manual_edits(prev, current, new_auto)
    assert result.text == "Chief complaint: red eye x3 days.\n- A\n- B"
    assert result.frozen is False


def test_merge_splices_new_auto_block_in_place_when_manual_text_wraps_it():
    prev = "- A"
    current = "Note above.\n- A\nNote below."
    new_auto = "- A\n- B"
    result = merge_manual_edits(prev, current, new_auto)
    assert result.text == "Note above.\n- A\n- B\nNote below."


def test_merge_freezes_when_clinician_edits_inside_the_auto_block_itself():
    prev = "- A\n- B"
    current = "- A (resolved)\n- B"  # user retyped the first bullet
    new_auto = "- A\n- B\n- C"       # a new checkbox was also clicked
    result = merge_manual_edits(prev, current, new_auto)
    assert result.text == current  # untouched -- never clobber a human edit to clinical text
    assert result.frozen is True


def test_merge_is_idempotent_when_nothing_manual_was_added():
    prev = "- A"
    current = "- A"
    new_auto = "- A\n- B"
    result = merge_manual_edits(prev, current, new_auto)
    assert result.text == "- A\n- B"
    assert result.frozen is False

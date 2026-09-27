"""Return-visit recommendation carry-forward (user request, 2026-09):
a provider's "come back in N units for these tests, because X" is captured
once, on the exam (EyeExam.follow_up_weeks/follow_up_unit/follow_up_reason
plus zero or more DiagnosticOrder rows sharing that exam's id), and this
module is the single read path everything else builds on -- the Patient
Overview "Recommended Follow-Up" card and the booking-form prefill both
call `get_pending_followups` rather than re-deriving the same query.

Deliberately exam-scoped, not per-test: a doctor recommending three tests
for one reason should read as one recommendation with three tests, not
three separate alerts saying the same thing -- unlike the look-back engine's
per-test-code alerts (ehr.services.lookback_alerts), which answer a
different question ("is this specific test overdue for this condition").
The two are independent and intentionally not merged: this module tracks a
provider's explicit one-time instruction; lookback_alerts tracks an
ongoing chronic-condition testing cadence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ehr.models.database import EyeExam, DiagnosticOrder, DiagnosticTest

# Rough calendar-math approximation (Month=30 days, Year=365 days) -- a
# "suggested" prefill date for staff to adjust, not a billing- or
# compliance-grade interval calculation. Matches this app's existing
# "narrow, simple approximation over exact calendar math" posture.
UNIT_TO_DAYS = {"Day": 1, "Week": 7, "Month": 30, "Year": 365}


@dataclass
class PendingFollowup:
    exam_id: int
    exam_date: str
    provider_name: str
    reason: str
    interval_label: str  # e.g. "3 Months"
    suggested_date: str  # 'YYYY-MM-DD'
    tests: list = field(default_factory=list)  # [{"id": int, "abbreviation": str}]


def suggest_followup_date(exam_date_str: str, weeks: int, unit: str) -> str:
    """exam_date_str is 'YYYY-MM-DD'. Falls back to today if exam_date_str
    is malformed -- this is a prefill convenience, never a hard requirement,
    so a bad date shouldn't block anything downstream."""
    try:
        base = datetime.strptime(exam_date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        base = date.today()
    days = (weeks or 0) * UNIT_TO_DAYS.get(unit or "Week", 7)
    return (base + timedelta(days=days)).isoformat()


def get_pending_followups(db, patient_id: int) -> list[PendingFollowup]:
    """Every exam for this patient with an unaddressed follow-up
    recommendation (follow_up_status == 'pending'), newest first, each
    carrying its still-outstanding recommended tests (if any)."""
    exams = (db.query(EyeExam)
        .filter(EyeExam.patient_id == patient_id, EyeExam.follow_up_status == "pending",
                EyeExam.follow_up_weeks.isnot(None))
        .order_by(EyeExam.exam_date.desc()).all())
    if not exams:
        return []
    orders = (db.query(DiagnosticOrder, DiagnosticTest)
        .join(DiagnosticTest, DiagnosticTest.id == DiagnosticOrder.diagnostic_test_id)
        .filter(DiagnosticOrder.ordered_exam_id.in_([e.id for e in exams]),
                DiagnosticOrder.status == "ordered")
        .all())
    tests_by_exam = {}
    for order, test in orders:
        tests_by_exam.setdefault(order.ordered_exam_id, []).append(
            {"id": test.id, "abbreviation": test.calendar_abbreviation})

    results = []
    for exam in exams:
        provider = exam.provider
        results.append(PendingFollowup(
            exam_id=exam.id,
            exam_date=exam.exam_date,
            provider_name=f"Dr. {provider.last_name}" if provider else "Unknown provider",
            reason=exam.follow_up_reason or "",
            interval_label=f"{exam.follow_up_weeks} {(exam.follow_up_unit or 'Week')}"
                            f"{'s' if exam.follow_up_weeks != 1 else ''}",
            suggested_date=suggest_followup_date(exam.exam_date, exam.follow_up_weeks, exam.follow_up_unit),
            tests=tests_by_exam.get(exam.id, []),
        ))
    return results

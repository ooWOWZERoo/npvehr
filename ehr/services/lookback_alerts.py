"""Look-back & clinical alert engine (Phase 4 of the chief-complaint/CPT/
billing-flow plan, BUILD_BACKLOG.md 0a). No new schema -- pure queries over
Phase 3's `diagnostic_orders` table and the existing `Problem` list, run
synchronously per page load (this app has no generic scheduled-job runner
beyond one specific cron-secret-gated endpoint, so a background evaluation
job isn't a fit here).

Two independent checks, matching the original request:
1. Outstanding orders -- any DiagnosticOrder still ordered/scheduled/
   in_progress for this patient.
2. Chronic-condition testing-interval compliance -- for each Active Problem
   matching a condition profile below, whether the most recent *completed*
   order for each of that profile's required tests falls within the
   required interval.

CONDITION_PROFILES is a small, curated list of dicts (the same "narrow
hardcoded lookup, not a real rules engine" posture already established for
ICD-10 in ap_composer.py and CPT in cpt_mapper.py) -- not a configurable
admin-managed rules table. `Problem.icd10_code` and `.severity_or_stage` are
both free text (this app's established ICD-10 treatment, deferred real
terminology-server integration per VISION_EHR_DATA_STANDARDS_RESEARCH.md
4.4), so matching is a simple prefix/substring check, not exact lookup.

Deliberately uses DiagnosticOrder.completed_at as the *only* source of truth
for "when was test X last done" -- GlaucomaTracking's own follow_up_interval/
diagnostic_orders fields are free text tied only to that exam's date, not a
structured, queryable completion signal, and tracking two "last done" clocks
that could silently disagree would be worse than tracking one.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from ehr.models.database import DiagnosticOrder, DiagnosticTest, Problem

OUTSTANDING_STATUSES = ("ordered", "scheduled", "in_progress")

# Each profile: icd10_prefixes (matched via str.startswith against
# Problem.icd10_code, case-insensitive), label, required_test_codes (from
# the diagnostic_tests catalog), default_interval_days, and an optional
# severity_interval_days dict whose keys are matched as substrings against
# Problem.severity_or_stage (case-insensitive) -- first key that matches
# wins; falls back to default_interval_days when none match or
# severity_or_stage is blank.
CONDITION_PROFILES = [
    {
        "icd10_prefixes": ["H40."],
        "label": "Standard Glaucoma Protocol",
        "required_test_codes": ["VF", "OCT"],
        "default_interval_days": 365,
        "severity_interval_days": {"moderate": 182, "severe": 182},
    },
    {
        "icd10_prefixes": ["Z79.899"],
        "label": "Plaquenil Retinal Toxicity Monitoring",
        "required_test_codes": ["VF", "OCT"],
        "default_interval_days": 365,
        "severity_interval_days": None,
    },
]


def _match_profile(problem: Problem):
    code = (problem.icd10_code or "").upper()
    if not code:
        return None
    for profile in CONDITION_PROFILES:
        if any(code.startswith(prefix.upper()) for prefix in profile["icd10_prefixes"]):
            return profile
    return None


def _interval_days_for(problem: Problem, profile: dict) -> int:
    severity_map = profile.get("severity_interval_days")
    if severity_map:
        text = (problem.severity_or_stage or "").lower()
        for keyword, days in severity_map.items():
            if keyword in text:
                return days
    return profile["default_interval_days"]


def _months_ago(dt: datetime) -> int:
    return max(0, round((datetime.utcnow() - dt).days / 30))


def get_alerts_for_patient(db, patient_id: int) -> list[dict]:
    """Returns a list of {type, severity, message, order_id?, problem_id?,
    test_code?} dicts. `type` is 'outstanding_order' or 'interval_due';
    `severity` is 'warning' (outstanding_order) or 'info' (interval_due),
    matching this app's existing two-tier alert-banner palette
    (.alert-warning/.alert-info)."""
    alerts = []

    outstanding = (db.query(DiagnosticOrder)
        .filter(DiagnosticOrder.patient_id == patient_id, DiagnosticOrder.status.in_(OUTSTANDING_STATUSES))
        .order_by(DiagnosticOrder.ordered_at).all())
    for order in outstanding:
        ordered_date = order.ordered_at.strftime("%m/%d/%Y") if order.ordered_at else "an earlier visit"
        alerts.append({
            "type": "outstanding_order", "severity": "warning",
            "message": f"Outstanding order: {order.diagnostic_test.display_name} from {ordered_date} is pending.",
            "order_id": order.id,
        })
    # A test already covered by an outstanding order (above) shouldn't also
    # get an "interval due" nag below -- ordering it is already the correct
    # response, and showing both is exactly the alert-fatigue noise this
    # engine is meant to avoid.
    outstanding_test_ids = {o.diagnostic_test_id for o in outstanding}

    active_problems = db.query(Problem).filter(Problem.patient_id == patient_id, Problem.status == "Active").all()
    for problem in active_problems:
        profile = _match_profile(problem)
        if not profile:
            continue
        interval_days = _interval_days_for(problem, profile)
        for test_code in profile["required_test_codes"]:
            test = db.query(DiagnosticTest).filter(DiagnosticTest.code == test_code).first()
            if not test or test.id in outstanding_test_ids:
                continue
            most_recent = (db.query(DiagnosticOrder)
                .filter(DiagnosticOrder.patient_id == patient_id, DiagnosticOrder.diagnostic_test_id == test.id,
                        DiagnosticOrder.status == "completed")
                .order_by(DiagnosticOrder.completed_at.desc()).first())
            if most_recent is None:
                message = f"{profile['label']}: {test.display_name} due (never completed)."
            elif most_recent.completed_at < datetime.utcnow() - timedelta(days=interval_days):
                message = (f"{profile['label']}: {test.display_name} due "
                    f"(last done: {_months_ago(most_recent.completed_at)} months ago).")
            else:
                continue
            alerts.append({
                "type": "interval_due", "severity": "info", "message": message,
                "problem_id": problem.id, "test_code": test_code,
            })

    return alerts

"""CPT mapping + two-flow billing preview (Phase 2 of the chief-complaint/
CPT/billing-flow plan, BUILD_BACKLOG.md 0a) -- computes a read-only,
staff-facing billing preview from data already on file. This app has no
billing/claims infrastructure at all; nothing this module produces is ever
transmitted, submitted, or persisted as a real claim -- it exists purely to
show staff what a visit's codes would look like, the same "narrow curated
lookup, not a real engine" posture already used for ICD-10 in
ehr.services.ap_composer.

Unlike ap_composer, this module is not UI-independent/pure -- it reads the
database directly (joining Appointment/AppointmentTest/DiagnosticTest/
PatientInsurancePlan), since there is no equivalent client-side mirror of
this logic (the billing preview is server-rendered on page load, not a
live-typing suggestion like the New Exam form's composers).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from sqlalchemy.orm import Session

from ehr.models.database import Appointment, AppointmentTest, DiagnosticTest, CptCode, PatientInsurancePlan


@dataclass
class CptLineItem:
    code: str
    description: str


@dataclass
class CptSummary:
    visit_flow: Optional[str]  # 'vision' | 'medical' | None (undetermined -- not yet checked in)
    exam_code: Optional[CptLineItem] = None
    refraction_code: Optional[CptLineItem] = None
    testing_codes: list[CptLineItem] = field(default_factory=list)
    em_code: Optional[str] = None
    diagnosis_codes: list[str] = field(default_factory=list)
    medical_payer_name: Optional[str] = None
    vision_payer_name: Optional[str] = None


def _cpt_description(db: Session, code: str) -> str:
    row = db.query(CptCode).filter(CptCode.code == code).first()
    return row.description if row else code


def compute_cpt_summary(db: Session, exam) -> Optional[CptSummary]:
    """None if this exam has no linked appointment -- a walk-in exam entered
    directly, or any exam from before Phase 2 -- since there's no visit-flow
    context to build a preview from."""
    if not exam.appointment_id:
        return None
    appt = db.query(Appointment).filter(Appointment.id == exam.appointment_id).first()
    if not appt:
        return None

    relationship = appt.patient_relationship_at_booking or "established"
    # Comprehensive exam code is relationship-driven regardless of flow --
    # both a routine vision visit and a medical visit use the same 92004/92014
    # split; only whether 92015 (refraction) rides along on the same claim or
    # gets billed separately to the patient depends on visit_flow (see the
    # exams/detail.html billing-preview card, which renders the two flows
    # differently using this same exam_code/refraction_code pair).
    exam_cpt = "92004" if relationship == "new" else "92014"
    exam_code = CptLineItem(exam_cpt, _cpt_description(db, exam_cpt))
    refraction_code = CptLineItem("92015", _cpt_description(db, "92015"))

    testing_rows = (db.query(DiagnosticTest.cpt_code)
        .join(AppointmentTest, AppointmentTest.diagnostic_test_id == DiagnosticTest.id)
        .filter(AppointmentTest.appointment_id == appt.id, AppointmentTest.status == "active",
                DiagnosticTest.cpt_code.isnot(None))
        .distinct().all())
    testing_codes = [CptLineItem(row[0], _cpt_description(db, row[0])) for row in testing_rows]

    diagnosis_codes = [c.strip() for c in (exam.diagnosis_codes or "").split(",") if c.strip()]
    em_code = exam.em_code_confirmed or exam.suggested_em_code

    medical_plan = (db.query(PatientInsurancePlan)
        .filter(PatientInsurancePlan.patient_id == appt.patient_id,
                PatientInsurancePlan.plan_category == "medical", PatientInsurancePlan.is_active == True)
        .order_by(PatientInsurancePlan.id.desc()).first())
    vision_plan = (db.query(PatientInsurancePlan)
        .filter(PatientInsurancePlan.patient_id == appt.patient_id,
                PatientInsurancePlan.plan_category == "vision", PatientInsurancePlan.is_active == True)
        .order_by(PatientInsurancePlan.id.desc()).first())

    return CptSummary(visit_flow=appt.visit_flow, exam_code=exam_code, refraction_code=refraction_code,
        testing_codes=testing_codes, em_code=em_code, diagnosis_codes=diagnosis_codes,
        medical_payer_name=medical_plan.payer_name if medical_plan else None,
        vision_payer_name=vision_plan.payer_name if vision_plan else None)


def suggest_visit_flow(db: Session, patient_id: int) -> str:
    """Check-in-time auto-suggestion: an active medical plan on file suggests
    'medical' (the more constrained flow -- refraction must be billed
    separately to the patient even when a vision plan also exists on the
    same chart); otherwise defaults to 'vision', the common case for routine
    traffic, so front desk isn't left with a blank required field."""
    has_medical = (db.query(PatientInsurancePlan)
        .filter(PatientInsurancePlan.patient_id == patient_id,
                PatientInsurancePlan.plan_category == "medical", PatientInsurancePlan.is_active == True)
        .first())
    return "medical" if has_medical else "vision"

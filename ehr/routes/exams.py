from datetime import date
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from ehr.models.database import get_db, EyeExam, Refraction, AnteriorSegmentAssessment, GlaucomaTracking, BinocularVisionAssessment, Patient, Provider
from ehr.env_info import EHR_ENV
from ehr.utils import patient_context
from ehr.auth.permissions import require_role, EXAM_VIEW, EXAM_EDIT, ROLE_LABELS

router = APIRouter(prefix="/exams", tags=["exams"])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS

def _f(v):
    try: return float(v) if v and str(v).strip() else None
    except: return None
def _i(v):
    try: return int(v) if v and str(v).strip() else None
    except: return None
def _b(v):
    # Tri-state: "Yes"/"No" dropdown, not a checkbox -- an unset finding is
    # clinically different from a confirmed-absent one.
    return {"Yes": True, "No": False}.get(v)

@router.get("/new", response_class=HTMLResponse, dependencies=[Depends(require_role(*EXAM_EDIT))])
def new_exam_form(request: Request, patient_id: int = None, db: Session = Depends(get_db)):
    ctx_patient = patient_context(db.query(Patient).filter(Patient.id == patient_id).first()) if patient_id else None
    return templates.TemplateResponse(request, "exams/form.html", {
        "patients": db.query(Patient).order_by(Patient.last_name).all(),
        "providers": db.query(Provider).all(),
        "selected_patient_id": patient_id, "today": str(date.today()), "context_patient": ctx_patient})

@router.post("/new", dependencies=[Depends(require_role(*EXAM_EDIT))])
async def create_exam(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    g = lambda k: form.get(k, "")
    gl = lambda k: ", ".join(form.getlist(k))  # comma-join a multi-value (checkbox) field
    exam = EyeExam(
        patient_id=int(g("patient_id")), provider_id=int(g("provider_id")),
        exam_date=g("exam_date"), chief_complaint=g("chief_complaint"),
        od_sc=g("od_sc"), os_sc=g("os_sc"), od_cc=g("od_cc"), os_cc=g("os_cc"),
        iop_od=_f(g("iop_od")), iop_os=_f(g("iop_os")), iop_method=g("iop_method"),
        cover_test=g("cover_test"),
        sl_lids_od=g("sl_lids_od"), sl_lids_os=g("sl_lids_os"),
        sl_cornea_od=g("sl_cornea_od"), sl_cornea_os=g("sl_cornea_os"),
        sl_lens_od=g("sl_lens_od"), sl_lens_os=g("sl_lens_os"),
        fundus_disc_od=g("fundus_disc_od"), fundus_disc_os=g("fundus_disc_os"),
        fundus_macula_od=g("fundus_macula_od"), fundus_macula_os=g("fundus_macula_os"),
        fundus_vessels_od=g("fundus_vessels_od"), fundus_vessels_os=g("fundus_vessels_os"),
        fundus_periphery_od=g("fundus_periphery_od"), fundus_periphery_os=g("fundus_periphery_os"),
        assessment=g("assessment"), plan=g("plan"),
        diagnosis_codes=g("diagnosis_codes"), follow_up_weeks=_i(g("follow_up_weeks")),
        refractive_diagnosis=gl("refractive_diagnosis"), refractive_laterality=g("refractive_laterality"),
        refractive_stability=g("refractive_stability"), refractive_secondary_findings=gl("refractive_secondary_findings"))
    db.add(exam); db.flush()
    # Three-step refraction matrix (IHE GEE): habitual (current glasses as worn
    # in), manifest (subjective refinement), cycloplegic (post-dilation). Each
    # is optional and independent -- a Refraction row is only created for a
    # type if at least one sphere value was actually entered for it.
    for prefix, rtype in (("hab", "habitual"), ("man", "manifest"), ("cyc", "cycloplegic")):
        od_sphere, os_sphere = _f(g(f"{prefix}_od_sphere")), _f(g(f"{prefix}_os_sphere"))
        if od_sphere is None and os_sphere is None:
            continue
        db.add(Refraction(exam_id=exam.id, refraction_type=rtype,
            od_sphere=od_sphere, od_cylinder=_f(g(f"{prefix}_od_cylinder")),
            od_axis=_i(g(f"{prefix}_od_axis")), od_add=_f(g(f"{prefix}_od_add")), od_va=g(f"{prefix}_od_va"),
            os_sphere=os_sphere, os_cylinder=_f(g(f"{prefix}_os_cylinder")),
            os_axis=_i(g(f"{prefix}_os_axis")), os_add=_f(g(f"{prefix}_os_add")), os_va=g(f"{prefix}_os_va")))
    # Anterior Segment / Dry Eye assessment (5.2) -- only created if at least
    # one of its fields was actually filled in, same "any subset, all
    # optional" rule as the refraction rows above.
    asa_fields = dict(
        primary_diagnosis_code=g("asa_primary_diagnosis_code"), severity=g("asa_severity"),
        conjunctival_injection_od=g("asa_conjunctival_injection_od"), conjunctival_injection_os=g("asa_conjunctival_injection_os"),
        corneal_staining_od=g("asa_corneal_staining_od"), corneal_staining_os=g("asa_corneal_staining_os"),
        mgd_expression_od=g("asa_mgd_expression_od"), mgd_expression_os=g("asa_mgd_expression_os"),
        tbut_seconds_od=_i(g("asa_tbut_seconds_od")), tbut_seconds_os=_i(g("asa_tbut_seconds_os")),
        schirmer_mm_od=_i(g("asa_schirmer_mm_od")), schirmer_mm_os=_i(g("asa_schirmer_mm_os")),
        plan_therapeutics=gl("asa_plan_therapeutics"), follow_up_interval=g("asa_follow_up_interval"),
        clinical_notes=g("asa_clinical_notes"))
    if any(v not in (None, "") for v in asa_fields.values()):
        db.add(AnteriorSegmentAssessment(exam_id=exam.id, **asa_fields))
    # Posterior Segment / Glaucoma tracking (5.3) -- same all-optional rule.
    gt_fields = dict(
        primary_diagnosis_code=g("gt_primary_diagnosis_code"),
        target_iop_od=_i(g("gt_target_iop_od")), target_iop_os=_i(g("gt_target_iop_os")),
        iop_current_od=_i(g("gt_iop_current_od")), iop_current_os=_i(g("gt_iop_current_os")),
        iop_time_measured=g("gt_iop_time_measured"), iop_method=g("gt_iop_method"),
        cup_disc_ratio_od=_f(g("gt_cup_disc_ratio_od")), cup_disc_ratio_os=_f(g("gt_cup_disc_ratio_os")),
        nerve_tissue_status_od=g("gt_nerve_tissue_status_od"), nerve_tissue_status_os=g("gt_nerve_tissue_status_os"),
        oct_rnfl_average_microns_od=_i(g("gt_oct_rnfl_average_microns_od")), oct_rnfl_average_microns_os=_i(g("gt_oct_rnfl_average_microns_os")),
        visual_field_md_db_od=_f(g("gt_visual_field_md_db_od")), visual_field_md_db_os=_f(g("gt_visual_field_md_db_os")),
        vf_reliability_od=g("gt_vf_reliability_od"), vf_reliability_os=g("gt_vf_reliability_os"),
        prescribed_glaucoma_meds=gl("gt_prescribed_glaucoma_meds"), diagnostic_orders=gl("gt_diagnostic_orders"),
        follow_up_interval=g("gt_follow_up_interval"), clinical_notes=g("gt_clinical_notes"))
    if any(v not in (None, "") for v in gt_fields.values()):
        db.add(GlaucomaTracking(exam_id=exam.id, **gt_fields))
    # Binocular Vision & Pediatrics (Vision Therapy) assessment (5.4) -- same
    # all-optional rule.
    bv_fields = dict(
        primary_diagnosis_code=g("bv_primary_diagnosis_code"),
        phoria_distance_diopters=_i(g("bv_phoria_distance_diopters")), phoria_near_diopters=_i(g("bv_phoria_near_diopters")),
        strabismus_present=_b(g("bv_strabismus_present")), strabismus_direction=g("bv_strabismus_direction"),
        npc_break_cm=_f(g("bv_npc_break_cm")), npc_recovery_cm=_f(g("bv_npc_recovery_cm")),
        accommodation_amplitude_od=_f(g("bv_accommodation_amplitude_od")), accommodation_amplitude_os=_f(g("bv_accommodation_amplitude_os")),
        assigned_home_exercises=gl("bv_assigned_home_exercises"),
        therapy_session_number=_i(g("bv_therapy_session_number")), therapy_compliance_rating=g("bv_therapy_compliance_rating"),
        follow_up_interval=g("bv_follow_up_interval"), clinical_notes=g("bv_clinical_notes"))
    if any(v not in (None, "") for v in bv_fields.values()):
        db.add(BinocularVisionAssessment(exam_id=exam.id, **bv_fields))
    db.commit()
    return RedirectResponse(f"/exams/{exam.id}", status_code=303)

@router.get("/{exam_id}", response_class=HTMLResponse, dependencies=[Depends(require_role(*EXAM_VIEW))])
def exam_detail(request: Request, exam_id: int, db: Session = Depends(get_db)):
    e = db.query(EyeExam).filter(EyeExam.id == exam_id).first()
    if not e: return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "exams/detail.html",
        {"exam": e, "context_patient": patient_context(e.patient)})

from datetime import date, datetime
from typing import List
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from ehr.models.database import (get_db, EyeExam, EyeExamAddendum, Refraction, DryEyeAssessment, AnteriorSegmentAssessment,
    GlaucomaTracking, BinocularVisionAssessment, SurgeryComanagementTracking, Patient, Problem, ProblemAddendum,
    DiagnosticTest, DiagnosticOrder, EyeExamFollowUp)
from ehr.env_info import EHR_ENV
from ehr.utils import patient_context
from ehr.auth.deps import get_current_user
from ehr.auth.permissions import require_role, EXAM_VIEW, EXAM_EDIT, EXAM_SIGN, ROLE_LABELS, BILLING_OVERRIDE
from ehr.auth import csrf
from ehr.services import cpt_mapper
from ehr.services import lookback_alerts
from ehr.services import ncci_edits
from ehr.services import scheduling as sched
from ehr.services import authz
from ehr.services import billing_override
from ehr.services import billing_ledger
from ehr.services import ros_responses
from ehr.services import medications as med_svc
from ehr.services import pcp_letter
from ehr.services import safety as safety_svc

router = APIRouter(prefix="/exams", tags=["exams"])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS
templates.env.globals["FOCUS_SECTION_MAP"] = lookback_alerts.TEST_CODE_TO_FOCUS_SECTION

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

# The Glaucoma dashboard's "Diagnostic Orders" checkboxes submit a
# diagnostic_tests catalog code (Phase 3, BUILD_BACKLOG.md 0a -- needed to
# create real DiagnosticOrder rows below), but GlaucomaTracking.diagnostic_orders
# is a free-text display field on the exam detail page, so this maps back to
# the human-readable label for that column (exams/form.html's checkboxes
# carry the same mapping in their data-label attribute for the live Plan
# preview -- kept in sync by hand, both are small and rarely change).
GT_DIAGNOSTIC_ORDER_LABELS = {"OCT_ONH": "OCT RNFL", "VF": "Humphrey VF 24-2",
    "GONIOSCOPY": "Gonioscopy", "PACHYMETRY": "Pachymetry"}

def _diagnostic_tests(db: Session):
    return db.query(DiagnosticTest).filter(DiagnosticTest.active == True).order_by(DiagnosticTest.display_order).all()

def _ncci_pairs_json():
    return [{"column1": e.column1, "column2": e.column2,
             "modifier_indicator": e.modifier_indicator, "message": e.message}
            for e in ncci_edits.PTP_EDITS]

def _create_diagnostic_orders(db: Session, exam: EyeExam, user_id: int, codes: list, notes: str = None):
    """Creates a real, patient-scoped DiagnosticOrder (status='ordered') per
    catalog code, tied back to this exam via ordered_exam_id. Shared by the
    Glaucoma dashboard's "Diagnostic Orders" checkboxes (notes=None, as
    before) and the general "Recommended Follow-Up" checklist below (notes=
    the provider's stated reason, so it travels with the order and shows up
    wherever that order is later surfaced) -- same underlying mechanism
    regardless of which visit section a test was recommended from.
    Unknown/deactivated codes are silently skipped rather than failing the
    whole exam save."""
    test_by_code = {t.code: t for t in db.query(DiagnosticTest).filter(DiagnosticTest.code.in_(codes)).all()}
    for code in codes:
        test = test_by_code.get(code)
        if test:
            db.add(DiagnosticOrder(patient_id=exam.patient_id, diagnostic_test_id=test.id,
                ordered_by_user_id=user_id, ordered_exam_id=exam.id, notes=notes))

def _parse_return_visit_block(g, form, suffix: str):
    """One of up to two independent return-visit recommendations on the New
    Exam form (user request: "allow for up to two return visit planning").
    suffix is '' for the first, always-visible block, or '_2' for the
    second, hidden behind "Add another return visit" until used -- same
    field names either way, just suffixed, rather than a fully dynamic/
    JS-cloned field list, since the count is capped at exactly two.
    Returns None if the block is entirely empty (nothing recommended);
    otherwise raises ValueError if a reason is missing (checked here, before
    the exam even exists, same as the single-block version this replaces),
    or returns the block's parsed fields for the caller to act on once the
    exam has an id."""
    weeks = _i(g(f"follow_up_weeks{suffix}"))
    codes = form.getlist(f"return_visit_diagnostic_orders{suffix}")
    if not weeks and not codes:
        return None
    reason = g(f"follow_up_reason{suffix}").strip()
    if not reason:
        label = "Return Visit 2" if suffix else "Return Visit"
        raise ValueError(f"{label}: a reason is required whenever a return interval or a follow-up test is set.")
    return {
        "weeks": weeks, "unit": g(f"follow_up_unit{suffix}") or "Week", "reason": reason, "codes": codes,
        "override_checked": form.get(f"followup_billing_override{suffix}") == "true",
        "override_reason": g(f"followup_billing_override_reason{suffix}"),
    }

@router.get("/new", response_class=HTMLResponse, dependencies=[Depends(require_role(*EXAM_EDIT))])
def new_exam_form(request: Request, patient_id: int = None, appointment_id: int = None, db: Session = Depends(get_db)):
    ctx_patient = patient_context(db.query(Patient).filter(Patient.id == patient_id).first(), db) if patient_id else None
    # Active problems for the "Problems Addressed" checklist -- only meaningful
    # once a patient is already known (reached via the patient workspace's
    # "New Exam" link, matching how context_patient/selected_patient_id
    # already work elsewhere in this form).
    active_problems = (db.query(Problem).filter(Problem.patient_id == patient_id, Problem.status == "Active")
                        .order_by(Problem.diagnosis_name).all()) if patient_id else []
    # Look-back & clinical alert engine (Phase 4, BUILD_BACKLOG.md 0a) --
    # surfaced here too, not just the patient overview tab, since a clinician
    # about to document a visit is exactly when an overdue chronic-disease
    # test or a still-outstanding order is most actionable.
    lookback_alerts_list = lookback_alerts.get_alerts_for_patient(db, patient_id) if patient_id else []
    return templates.TemplateResponse(request, "exams/form.html", {
        "patients": db.query(Patient).order_by(Patient.last_name).all(),
        "providers": sched.bookable_providers(db),
        "selected_patient_id": patient_id, "today": str(date.today()), "context_patient": ctx_patient,
        "active_problems": active_problems, "appointment_id": appointment_id,
        "lookback_alerts": lookback_alerts_list,
        "tests": _diagnostic_tests(db), "ncci_pairs": _ncci_pairs_json(),
        "can_override_billing": request.state.user.role in BILLING_OVERRIDE})

@router.post("/new", dependencies=[Depends(require_role(*EXAM_EDIT))])
async def create_exam(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    csrf.verify_or_403(request.state.csrf_token, form.get("csrf_token"))
    g = lambda k: form.get(k, "")
    gl = lambda k: ", ".join(form.getlist(k))  # comma-join a multi-value (checkbox) field
    # Return-visit recommendation carry-forward (user request): a bare
    # interval or a checked test with no stated reason is exactly the kind
    # of ambiguous instruction that causes front-desk missteps downstream,
    # so this is the one hard server-side requirement in an otherwise
    # all-optional form. Checked before the exam even exists (same as
    # before) since neither block needs the exam's id yet.
    try:
        return_visit_blocks = [b for b in (
            _parse_return_visit_block(g, form, ""), _parse_return_visit_block(g, form, "_2")) if b]
    except ValueError as e:
        return HTMLResponse(str(e), status_code=400)
    exam = EyeExam(
        patient_id=int(g("patient_id")), provider_id=int(g("provider_id")),
        appointment_id=_i(g("appointment_id")),
        exam_date=g("exam_date"), chief_complaint=g("chief_complaint"),
        od_sc=g("od_sc"), os_sc=g("os_sc"), od_cc=g("od_cc"), os_cc=g("os_cc"),
        pupil_size_light_od=_f(g("pupil_size_light_od")), pupil_size_light_os=_f(g("pupil_size_light_os")),
        pupil_size_dark_od=_f(g("pupil_size_dark_od")), pupil_size_dark_os=_f(g("pupil_size_dark_os")),
        pupil_size_near_od=_f(g("pupil_size_near_od")), pupil_size_near_os=_f(g("pupil_size_near_os")),
        pupil_reactivity_od=g("pupil_reactivity_od"), pupil_reactivity_os=g("pupil_reactivity_os"),
        pupil_apd_finding=g("pupil_apd_finding"), pupil_notes=g("pupil_notes"),
        motility_od=g("motility_od"), motility_os=g("motility_os"),
        confrontation_vf_od=g("confrontation_vf_od"), confrontation_vf_os=g("confrontation_vf_os"),
        iop_od=_f(g("iop_od")), iop_os=_f(g("iop_os")), iop_method=g("iop_method"),
        cover_test=g("cover_test"),
        sl_lids_od=g("sl_lids_od"), sl_lids_os=g("sl_lids_os"),
        sl_cornea_od=g("sl_cornea_od"), sl_cornea_os=g("sl_cornea_os"),
        sl_lens_od=g("sl_lens_od"), sl_lens_os=g("sl_lens_os"),
        fundus_disc_od=g("fundus_disc_od"), fundus_disc_os=g("fundus_disc_os"),
        cd_ratio_od=_f(g("cd_ratio_od")), cd_ratio_os=_f(g("cd_ratio_os")),
        fundus_macula_od=g("fundus_macula_od"), fundus_macula_os=g("fundus_macula_os"),
        fundus_vessels_od=g("fundus_vessels_od"), fundus_vessels_os=g("fundus_vessels_os"),
        fundus_periphery_od=g("fundus_periphery_od"), fundus_periphery_os=g("fundus_periphery_os"),
        vitreous_od=g("vitreous_od"), vitreous_os=g("vitreous_os"),
        ros_constitutional=_b(g("ros_constitutional")), ros_cardiovascular=_b(g("ros_cardiovascular")),
        ros_respiratory=_b(g("ros_respiratory")), ros_gastrointestinal=_b(g("ros_gastrointestinal")),
        ros_neurological=_b(g("ros_neurological")), ros_musculoskeletal=_b(g("ros_musculoskeletal")),
        ros_endocrine=_b(g("ros_endocrine")), ros_skin=_b(g("ros_skin")), ros_notes=g("ros_notes"),
        dilated_exam_performed=_b(g("dilated_exam_performed")), dilation_note=(g("dilation_note") or "").strip()[:255] or None,
        assessment=g("assessment"), plan=g("plan"),
        diagnosis_codes=g("diagnosis_codes"),
        refractive_diagnosis=gl("refractive_diagnosis"), refractive_laterality=g("refractive_laterality"),
        refractive_stability=g("refractive_stability"), refractive_secondary_findings=gl("refractive_secondary_findings"),
        suggested_exam_type=g("suggested_exam_type") or None, exam_type_confirmed=g("exam_type_confirmed") or None,
        suggested_em_code=g("suggested_em_code") or None, suggested_em_rationale=g("suggested_em_rationale") or None,
        em_code_confirmed=g("em_code_confirmed") or None)
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
    # Dry Eye / Ocular Surface Disease assessment (5.2, renamed from
    # "Anterior Segment" in v2.23 -- see DryEyeAssessment's docstring) --
    # only created if at least one of its fields was actually filled in,
    # same "any subset, all optional" rule as the refraction rows above.
    de_fields = dict(
        primary_diagnosis_code=g("de_primary_diagnosis_code"), severity=g("de_severity"),
        conjunctival_injection_od=g("de_conjunctival_injection_od"), conjunctival_injection_os=g("de_conjunctival_injection_os"),
        corneal_staining_od=g("de_corneal_staining_od"), corneal_staining_os=g("de_corneal_staining_os"),
        mgd_expression_od=g("de_mgd_expression_od"), mgd_expression_os=g("de_mgd_expression_os"),
        tbut_seconds_od=_i(g("de_tbut_seconds_od")), tbut_seconds_os=_i(g("de_tbut_seconds_os")),
        schirmer_mm_od=_i(g("de_schirmer_mm_od")), schirmer_mm_os=_i(g("de_schirmer_mm_os")),
        plan_therapeutics=gl("de_plan_therapeutics"), follow_up_interval=g("de_follow_up_interval"),
        clinical_notes=g("de_clinical_notes"))
    if any(v not in (None, "") for v in de_fields.values()):
        db.add(DryEyeAssessment(exam_id=exam.id, **de_fields))
    # Anterior Segment assessment (v2.23) -- a real structural exam of
    # conjunctiva/cornea/anterior chamber/iris/lens, separate from the dry-eye
    # dashboard above. Same all-optional rule.
    ant_fields = dict(
        primary_diagnosis_code=g("ant_primary_diagnosis_code"), severity=g("ant_severity"),
        conjunctival_injection_od=g("ant_conjunctival_injection_od"), conjunctival_injection_os=g("ant_conjunctival_injection_os"),
        conjunctival_discharge_od=g("ant_conjunctival_discharge_od"), conjunctival_discharge_os=g("ant_conjunctival_discharge_os"),
        conjunctival_follicles_papillae_od=g("ant_conjunctival_follicles_papillae_od"), conjunctival_follicles_papillae_os=g("ant_conjunctival_follicles_papillae_os"),
        conjunctival_chemosis_od=g("ant_conjunctival_chemosis_od"), conjunctival_chemosis_os=g("ant_conjunctival_chemosis_os"),
        corneal_epithelial_defect_od=g("ant_corneal_epithelial_defect_od"), corneal_epithelial_defect_os=g("ant_corneal_epithelial_defect_os"),
        corneal_edema_od=g("ant_corneal_edema_od"), corneal_edema_os=g("ant_corneal_edema_os"),
        corneal_infiltrate_od=g("ant_corneal_infiltrate_od"), corneal_infiltrate_os=g("ant_corneal_infiltrate_os"),
        corneal_arcus_od=g("ant_corneal_arcus_od"), corneal_arcus_os=g("ant_corneal_arcus_os"),
        corneal_guttata_od=g("ant_corneal_guttata_od"), corneal_guttata_os=g("ant_corneal_guttata_os"),
        pterygium_pinguecula_od=g("ant_pterygium_pinguecula_od"), pterygium_pinguecula_os=g("ant_pterygium_pinguecula_os"),
        van_herick_grade_od=g("ant_van_herick_grade_od"), van_herick_grade_os=g("ant_van_herick_grade_os"),
        ac_cells_flare_od=g("ant_ac_cells_flare_od"), ac_cells_flare_os=g("ant_ac_cells_flare_os"),
        iris_pattern_od=g("ant_iris_pattern_od"), iris_pattern_os=g("ant_iris_pattern_os"),
        iris_nvi_present_od=g("ant_iris_nvi_present_od"), iris_nvi_present_os=g("ant_iris_nvi_present_os"),
        iris_pi_status_od=g("ant_iris_pi_status_od"), iris_pi_status_os=g("ant_iris_pi_status_os"),
        lens_cataract_type_od=g("ant_lens_cataract_type_od"), lens_cataract_type_os=g("ant_lens_cataract_type_os"),
        lens_cataract_grade_od=g("ant_lens_cataract_grade_od"), lens_cataract_grade_os=g("ant_lens_cataract_grade_os"),
        plan_therapeutics=gl("ant_plan_therapeutics"), follow_up_interval=g("ant_follow_up_interval"),
        clinical_notes=g("ant_clinical_notes"))
    if any(v not in (None, "") for v in ant_fields.values()):
        db.add(AnteriorSegmentAssessment(exam_id=exam.id, **ant_fields))
    # Posterior Segment / Glaucoma tracking (5.3) -- same all-optional rule.
    gt_fields = dict(
        primary_diagnosis_code=g("gt_primary_diagnosis_code"), glaucoma_stage=g("gt_glaucoma_stage"),
        target_iop_od=_i(g("gt_target_iop_od")), target_iop_os=_i(g("gt_target_iop_os")),
        iop_current_od=_i(g("gt_iop_current_od")), iop_current_os=_i(g("gt_iop_current_os")),
        iop_time_measured=g("gt_iop_time_measured"), iop_method=g("gt_iop_method"),
        cup_disc_ratio_od=_f(g("gt_cup_disc_ratio_od")), cup_disc_ratio_os=_f(g("gt_cup_disc_ratio_os")),
        nerve_tissue_status_od=g("gt_nerve_tissue_status_od"), nerve_tissue_status_os=g("gt_nerve_tissue_status_os"),
        oct_rnfl_average_microns_od=_i(g("gt_oct_rnfl_average_microns_od")), oct_rnfl_average_microns_os=_i(g("gt_oct_rnfl_average_microns_os")),
        visual_field_md_db_od=_f(g("gt_visual_field_md_db_od")), visual_field_md_db_os=_f(g("gt_visual_field_md_db_os")),
        vf_reliability_od=g("gt_vf_reliability_od"), vf_reliability_os=g("gt_vf_reliability_os"),
        prescribed_glaucoma_meds=gl("gt_prescribed_glaucoma_meds"),
        diagnostic_orders=", ".join(GT_DIAGNOSTIC_ORDER_LABELS.get(c, c) for c in form.getlist("gt_diagnostic_orders")),
        follow_up_interval=g("gt_follow_up_interval"), clinical_notes=g("gt_clinical_notes"))
    if any(v not in (None, "") for v in gt_fields.values()):
        db.add(GlaucomaTracking(exam_id=exam.id, **gt_fields))
    # Diagnostic order tracking (Phase 3, BUILD_BACKLOG.md 0a): checking one
    # of the Glaucoma dashboard's diagnostic-order boxes also creates a real
    # DiagnosticOrder row (status='ordered'), patient-scoped so it persists
    # and can be checked at a later visit -- not just a free-text plan-line
    # note.
    _create_diagnostic_orders(db, exam, request.state.user.id, form.getlist("gt_diagnostic_orders"))
    # Return-visit recommendation carry-forward (user request, round 2 --
    # "allow for up to two return visit planning"): each non-empty block
    # becomes its own EyeExamFollowUp row plus the same real DiagnosticOrder
    # rows the Glaucoma dashboard's checkboxes create, tagged with that
    # block's own reason and follow_up_id so it travels with the order to
    # wherever it's next surfaced (patient overview card, booking-form
    # prefill, look-back alerts) and is attributable to the specific
    # recommendation it came from. NCCI billing-rule enforcement (user
    # request: "any exam that violates billing but is still requested can
    # be overridden by the OD(s) or General Manager only") runs per block,
    # against that block's own checked tests -- a flagged pair blocks the
    # save unless the current user can override and has done so.
    for block in return_visit_blocks:
        test_by_code = {t.code: t for t in db.query(DiagnosticTest).filter(DiagnosticTest.code.in_(block["codes"])).all()}
        block_tests = [test_by_code[c] for c in block["codes"] if c in test_by_code]
        cpt_codes = [t.cpt_code for t in block_tests if t.cpt_code]
        try:
            billing_override.enforce(db, request.state.user, cpt_codes,
                override_checked=block["override_checked"], override_reason=block["override_reason"],
                source="exam", record_id=exam.id)
        except billing_override.BillingRuleBlocked as e:
            db.rollback()
            return HTMLResponse(str(e), status_code=400)
        followup = EyeExamFollowUp(eye_exam_id=exam.id, follow_up_weeks=block["weeks"],
            follow_up_unit=block["unit"], reason=block["reason"])
        db.add(followup); db.flush()
        for t in block_tests:
            db.add(DiagnosticOrder(patient_id=exam.patient_id, diagnostic_test_id=t.id,
                ordered_by_user_id=request.state.user.id, ordered_exam_id=exam.id,
                follow_up_id=followup.id, notes=block["reason"]))
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
    # Pre-/Post-Operative Co-Management tracking (5.5) -- same all-optional
    # rule. One row per exam; the "timeline" (Pre-Op -> Day 1 -> Week 1 ...)
    # comes from a patient having one row per follow-up visit, not from a
    # mutable current_milestone field.
    sx_fields = dict(
        surgical_procedure=g("sx_surgical_procedure"), operative_eye=g("sx_operative_eye"),
        date_of_surgery=g("sx_date_of_surgery"), surgeon_name=g("sx_surgeon_name"),
        co_managing_facility=g("sx_co_managing_facility"), current_milestone=g("sx_current_milestone"),
        best_corrected_visual_acuity=g("sx_best_corrected_visual_acuity"),
        intraocular_pressure=_i(g("sx_intraocular_pressure")),
        corneal_edema_present=_b(g("sx_corneal_edema_present")), corneal_edema_grading=g("sx_corneal_edema_grading"),
        anterior_chamber_cells_flare=g("sx_anterior_chamber_cells_flare"),
        surgical_flap_or_wound_status=g("sx_surgical_flap_or_wound_status"),
        steroid_taper_schedule=g("sx_steroid_taper_schedule"),
        nsaid_drops_frequency=g("sx_nsaid_drops_frequency"), antibiotic_drops_status=g("sx_antibiotic_drops_status"),
        follow_up_interval=g("sx_follow_up_interval"), clinical_notes=g("sx_clinical_notes"))
    if any(v not in (None, "") for v in sx_fields.values()):
        db.add(SurgeryComanagementTracking(exam_id=exam.id, **sx_fields))
    # Problems Addressed (Problem List, first slice) -- checking one of the
    # patient's active problems and creating this exam appends a
    # ProblemAddendum linked to it, matching how a real visit-summary
    # document accumulates dated follow-up notes on a chronic diagnosis
    # across many visits, without re-entering the whole diagnosis each time.
    for problem_id in form.getlist("problems_addressed"):
        note = (g(f"problem_note_{problem_id}") or "").strip() or "Addressed at this visit."
        problem = db.query(Problem).filter(Problem.id == int(problem_id), Problem.patient_id == exam.patient_id).first()
        if problem:
            db.add(ProblemAddendum(problem_id=problem.id, exam_id=exam.id,
                author_user_id=request.state.user.id, note=note))
    billing_ledger.add_exam_charges(db, exam, request.state.user.id)
    # Granular ROS findings ticked in the ROS suggestions panel (ROS plan stage 4). Best-effort
    # and savepoint-guarded: a problem here can never stop the exam itself from saving.
    ros_responses.record_findings(db, exam.id, form.getlist("ros_finding"))
    # Medication / allergy list reviews confirmed on the form's medications card (best-effort, savepoint-guarded).
    med_svc.record_exam_reviews(db, exam, form, request.state.user.id)
    db.commit()
    return RedirectResponse(f"/exams/{exam.id}", status_code=303)

@router.get("/{exam_id}", response_class=HTMLResponse, dependencies=[Depends(require_role(*EXAM_VIEW))])
def exam_detail(request: Request, exam_id: int, db: Session = Depends(get_db)):
    e = db.query(EyeExam).filter(EyeExam.id == exam_id).first()
    if not e or not authz.can_view_patient(db, request.state.user, e.patient_id):
        return HTMLResponse("Not found", status_code=404)
    problem_addenda = db.query(ProblemAddendum).filter(ProblemAddendum.exam_id == exam_id).all()
    cpt_summary = cpt_mapper.compute_cpt_summary(db, e)
    return templates.TemplateResponse(request, "exams/detail.html",
        {"exam": e, "context_patient": patient_context(e.patient, db), "problem_addenda": problem_addenda,
         "cpt_summary": cpt_summary, "ros_findings": ros_responses.findings_for_exam(db, exam_id),
         "safety_fired": safety_svc.evaluate(db, e.patient_id, safety_svc.exam_text(e)),
         "safety_acks": safety_svc.acknowledgements_for_exam(db, exam_id),
         "safety_ack_required": request.query_params.get("safety_ack_required") == "1",
         "pcp_candidate": pcp_letter.candidate(db, e), "pcp_has_letter": pcp_letter.recent_status(db, e.patient_id),
         "pcp_error": request.query_params.get("pcp_error"), "med_reviews": med_svc.reviews_for_exam(db, exam_id)})


@router.post("/{exam_id}/sign", dependencies=[Depends(require_role(*EXAM_SIGN))])
def sign_exam(request: Request, exam_id: int, csrf_token: str = Form(""), ack_safety_rule: List[str] = Form([]),
              db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Clinical Record Sign/Lock/Amend Lifecycle: an electronic attestation
    ("I personally reviewed and stand behind this record"), matching the
    e-signature block real visit-summary documents already carry (spec
    §37.6/§18.2 item 4's long-tracked gap). Signing is one-way -- there is no
    unsign route -- and, since this app has no exam edit route at all
    (create + view-only), the only way to add anything further to a signed
    exam is EyeExamAddendum below."""
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    e = db.query(EyeExam).filter(EyeExam.id == exam_id).first()
    if not e: return HTMLResponse("Not found", status_code=404)
    if e.signed_at:
        return HTMLResponse("This visit is already signed.", status_code=400)
    # Clinical safety warnings (ROS plan stage 5): every warning that currently applies to this patient and exam
    # must be acknowledged by the signer. Saving the exam is never blocked -- only the sign-off waits.
    if safety_svc.unacknowledged(db, e, ack_safety_rule):
        return RedirectResponse(f"/exams/{exam_id}?safety_ack_required=1", status_code=303)
    safety_svc.record_acknowledgements(db, e, ack_safety_rule, user.id)
    e.signed_at = datetime.utcnow()
    e.signed_by_user_id = user.id
    db.commit()
    return RedirectResponse(f"/exams/{exam_id}", status_code=303)


@router.post("/{exam_id}/addenda", dependencies=[Depends(require_role(*EXAM_EDIT))])
def add_exam_addendum(request: Request, exam_id: int, note: str = Form(...), csrf_token: str = Form(""),
                       db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    e = db.query(EyeExam).filter(EyeExam.id == exam_id).first()
    if not e: return HTMLResponse("Not found", status_code=404)
    if not e.signed_at:
        return HTMLResponse("Only a signed visit can receive an addendum -- this one is still open for direct edits.", status_code=400)
    note = note.strip()
    if note:
        db.add(EyeExamAddendum(exam_id=exam_id, author_user_id=user.id, note=note))
        db.commit()
    return RedirectResponse(f"/exams/{exam_id}", status_code=303)

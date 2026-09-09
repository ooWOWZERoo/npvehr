from datetime import date
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from ehr.models.database import get_db, EyeExam, Refraction, Patient, Provider
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
        diagnosis_codes=g("diagnosis_codes"), follow_up_weeks=_i(g("follow_up_weeks")))
    db.add(exam); db.flush()
    if _f(g("od_sphere")) is not None or _f(g("os_sphere")) is not None:
        db.add(Refraction(exam_id=exam.id, refraction_type="manifest",
            od_sphere=_f(g("od_sphere")), od_cylinder=_f(g("od_cylinder")),
            od_axis=_i(g("od_axis")), od_add=_f(g("od_add")), od_va=g("od_va"),
            os_sphere=_f(g("os_sphere")), os_cylinder=_f(g("os_cylinder")),
            os_axis=_i(g("os_axis")), os_add=_f(g("os_add")), os_va=g("os_va")))
    db.commit()
    return RedirectResponse(f"/exams/{exam.id}", status_code=303)

@router.get("/{exam_id}", response_class=HTMLResponse, dependencies=[Depends(require_role(*EXAM_VIEW))])
def exam_detail(request: Request, exam_id: int, db: Session = Depends(get_db)):
    e = db.query(EyeExam).filter(EyeExam.id == exam_id).first()
    if not e: return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "exams/detail.html",
        {"exam": e, "context_patient": patient_context(e.patient)})

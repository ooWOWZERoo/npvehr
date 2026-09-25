from datetime import date, datetime
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from ehr.models.database import get_db, Prescription, PrescriptionAddendum, Patient
from ehr.env_info import EHR_ENV
from ehr.utils import patient_context
from ehr.auth.deps import get_current_user
from ehr.auth.permissions import require_role, RX_VIEW, RX_EDIT, RX_SIGN, ROLE_LABELS
from ehr.auth import csrf
from ehr.services import scheduling as sched

router = APIRouter(prefix="/prescriptions", tags=["prescriptions"])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS

def _f(v):
    try: return float(v) if v and str(v).strip() else None
    except: return None
def _i(v):
    try: return int(v) if v and str(v).strip() else None
    except: return None

@router.get("/new", response_class=HTMLResponse, dependencies=[Depends(require_role(*RX_EDIT))])
def new_rx_form(request: Request, patient_id: int = None, exam_id: int = None, db: Session = Depends(get_db)):
    ctx_patient = patient_context(db.query(Patient).filter(Patient.id == patient_id).first()) if patient_id else None
    return templates.TemplateResponse(request, "prescriptions/form.html", {
        "patients": db.query(Patient).order_by(Patient.last_name).all(),
        "providers": sched.bookable_providers(db),
        "selected_patient_id": patient_id, "selected_exam_id": exam_id, "today": str(date.today()),
        "context_patient": ctx_patient})

@router.post("/new", dependencies=[Depends(require_role(*RX_EDIT))])
async def create_rx(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    csrf.verify_or_403(request.state.csrf_token, form.get("csrf_token"))
    g = lambda k: form.get(k, "")
    gl = lambda k: ", ".join(form.getlist(k))  # comma-join a multi-value (checkbox) field
    rx = Prescription(
        patient_id=int(g("patient_id")), provider_id=int(g("provider_id")),
        exam_id=_i(g("exam_id")), rx_type=g("rx_type") or "glasses",
        issue_date=g("issue_date"), expiry_date=g("expiry_date"),
        od_sphere=_f(g("od_sphere")), od_cylinder=_f(g("od_cylinder")), od_axis=_i(g("od_axis")),
        od_add=_f(g("od_add")), od_prism=_f(g("od_prism")), od_base=g("od_base"),
        od_bc=_f(g("od_bc")), od_dia=_f(g("od_dia")), od_brand=g("od_brand"),
        os_sphere=_f(g("os_sphere")), os_cylinder=_f(g("os_cylinder")), os_axis=_i(g("os_axis")),
        os_add=_f(g("os_add")), os_prism=_f(g("os_prism")), os_base=g("os_base"),
        os_bc=_f(g("os_bc")), os_dia=_f(g("os_dia")), os_brand=g("os_brand"), notes=g("notes"),
        lens_type=g("lens_type"), lens_material=g("lens_material"), lens_treatments=gl("lens_treatments"),
        recall_interval=g("recall_interval"), patient_education_tags=gl("patient_education_tags"))
    db.add(rx); db.commit(); db.refresh(rx)
    return RedirectResponse(f"/prescriptions/{rx.id}", status_code=303)

@router.get("/{rx_id}", response_class=HTMLResponse, dependencies=[Depends(require_role(*RX_VIEW))])
def rx_detail(request: Request, rx_id: int, db: Session = Depends(get_db)):
    rx = db.query(Prescription).filter(Prescription.id == rx_id).first()
    if not rx: return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "prescriptions/detail.html",
        {"rx": rx, "context_patient": patient_context(rx.patient)})

@router.get("/{rx_id}/print", response_class=HTMLResponse, dependencies=[Depends(require_role(*RX_VIEW))])
def rx_print(request: Request, rx_id: int, db: Session = Depends(get_db)):
    rx = db.query(Prescription).filter(Prescription.id == rx_id).first()
    if not rx: return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "prescriptions/print.html", {"rx": rx})


@router.post("/{rx_id}/sign", dependencies=[Depends(require_role(*RX_SIGN))])
def sign_rx(request: Request, rx_id: int, csrf_token: str = Form(""),
            db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Clinical Record Sign/Lock/Amend Lifecycle, extended from EyeExam
    (ehr/routes/exams.py's sign_exam) -- same one-way electronic
    attestation, same PrescriptionAddendum-is-the-only-way-to-add-more
    pattern, since Prescription is create-only just like EyeExam."""
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    rx = db.query(Prescription).filter(Prescription.id == rx_id).first()
    if not rx: return HTMLResponse("Not found", status_code=404)
    if rx.signed_at:
        return HTMLResponse("This prescription is already signed.", status_code=400)
    rx.signed_at = datetime.utcnow()
    rx.signed_by_user_id = user.id
    db.commit()
    return RedirectResponse(f"/prescriptions/{rx_id}", status_code=303)


@router.post("/{rx_id}/addenda", dependencies=[Depends(require_role(*RX_EDIT))])
def add_rx_addendum(request: Request, rx_id: int, note: str = Form(...), csrf_token: str = Form(""),
                     db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    rx = db.query(Prescription).filter(Prescription.id == rx_id).first()
    if not rx: return HTMLResponse("Not found", status_code=404)
    if not rx.signed_at:
        return HTMLResponse("Only a signed prescription can receive an addendum -- this one is still open for direct edits.", status_code=400)
    note = note.strip()
    if note:
        db.add(PrescriptionAddendum(rx_id=rx_id, author_user_id=user.id, note=note))
        db.commit()
    return RedirectResponse(f"/prescriptions/{rx_id}", status_code=303)

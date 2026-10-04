"""Patient Medications & Allergies tab and its actions (see PATIENT_MEDICATION_LIST_DESIGN.md). Patient-scoped and
subject to record-level authorization (a provider only reaches their own patients; anything else is a 404). Writes need
MEDICATION_EDIT (clinical staff); everyone with access to the chart can view. Nothing here blocks an exam."""
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from ehr.auth import csrf
from ehr.auth.deps import get_current_user
from ehr.auth.permissions import MEDICATION_EDIT, require_role
from ehr.models.database import get_db
from ehr.models.medications import PatientAllergy, PatientMedication
from ehr.routes.patients import _get_patient_or_404, _workspace_ctx, templates
from ehr.services import medications as med_svc

router = APIRouter(prefix="/patients/{patient_id}/medications", tags=["medications"])
_EDIT = [Depends(require_role(*MEDICATION_EDIT))]


def _back(patient_id, msg=None):
    return RedirectResponse(f"/patients/{patient_id}/medications" + (f"?error={msg}" if msg else ""), status_code=303)


@router.get("", response_class=HTMLResponse)
def tab(request: Request, patient_id: int, db: Session = Depends(get_db)):
    p = _get_patient_or_404(db, patient_id, request.state.user)
    if not p:
        return HTMLResponse("Not found", status_code=404)
    ctx = _workspace_ctx(db, p, "medications")
    meds = db.query(PatientMedication).filter(PatientMedication.patient_id == patient_id).order_by(PatientMedication.name).all()
    active = [m for m in meds if m.status == "active"]
    classes = med_svc.classify(db, active)
    allergies = db.query(PatientAllergy).filter(PatientAllergy.patient_id == patient_id).order_by(PatientAllergy.allergen).all()
    ctx.update({
        "active_meds": active, "stopped_meds": [m for m in meds if m.status == "stopped"], "med_classes": classes,
        "unclassified": [m for m in active if not classes.get(m.id)],
        "active_allergies": [a for a in allergies if a.status == "active"], "inactive_allergies": [a for a in allergies if a.status == "inactive"],
        "med_review": med_svc.review_status(db, patient_id, "medications"), "allergy_review": med_svc.review_status(db, patient_id, "allergies"),
        "can_edit_meds": request.state.user.role in MEDICATION_EDIT, "error": request.query_params.get("error"),
        "stale_months": med_svc.STALE_MONTHS, "today": date.today(),
    })
    return templates.TemplateResponse(request, "patients/medications_tab.html", ctx)


@router.get("/summary.json")
def summary(request: Request, patient_id: int, db: Session = Depends(get_db)):
    """Read by the New Exam form's medications card. Patient-scoped: a provider only reaches their own patients (404 otherwise)."""
    p = _get_patient_or_404(db, patient_id, request.state.user)
    if not p:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return JSONResponse({**med_svc.exam_card_summary(db, patient_id), "can_edit": request.state.user.role in MEDICATION_EDIT},
                        headers={"Cache-Control": "no-store"})


async def _guard(request, db, patient_id):
    form = await request.form()
    csrf.verify_or_403(request.state.csrf_token, form.get("csrf_token"))
    p = _get_patient_or_404(db, patient_id, request.state.user)
    return form, p


@router.post("/new", dependencies=_EDIT)
async def add_med(request: Request, patient_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    form, p = await _guard(request, db, patient_id)
    if not p:
        return HTMLResponse("Not found", status_code=404)
    if not med_svc.add_medication(db, patient_id, user.id, form):
        return _back(patient_id, "A medication name is required.")
    db.commit()
    return _back(patient_id)


def _med(db, patient_id, med_id):
    m = db.get(PatientMedication, med_id)
    return m if m and m.patient_id == patient_id else None


@router.post("/{med_id}/edit", dependencies=_EDIT)
async def edit_med(request: Request, patient_id: int, med_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    form, p = await _guard(request, db, patient_id)
    m = _med(db, patient_id, med_id) if p else None
    if not m:
        return HTMLResponse("Not found", status_code=404)
    if not med_svc.update_medication(db, m, user.id, form):
        return _back(patient_id, "A medication name is required.")
    db.commit()
    return _back(patient_id)


@router.post("/{med_id}/stop", dependencies=_EDIT)
async def stop_med(request: Request, patient_id: int, med_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    form, p = await _guard(request, db, patient_id)
    m = _med(db, patient_id, med_id) if p else None
    if not m:
        return HTMLResponse("Not found", status_code=404)
    med_svc.set_medication_status(db, m, "stopped", user.id)
    db.commit()
    return _back(patient_id)


@router.post("/{med_id}/restart", dependencies=_EDIT)
async def restart_med(request: Request, patient_id: int, med_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    form, p = await _guard(request, db, patient_id)
    m = _med(db, patient_id, med_id) if p else None
    if not m:
        return HTMLResponse("Not found", status_code=404)
    med_svc.set_medication_status(db, m, "active", user.id)
    db.commit()
    return _back(patient_id)


@router.post("/allergies/new", dependencies=_EDIT)
async def add_allergy(request: Request, patient_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    form, p = await _guard(request, db, patient_id)
    if not p:
        return HTMLResponse("Not found", status_code=404)
    if not med_svc.add_allergy(db, patient_id, user.id, form):
        return _back(patient_id, "An allergen is required.")
    db.commit()
    return _back(patient_id)


def _allergy(db, patient_id, allergy_id):
    a = db.get(PatientAllergy, allergy_id)
    return a if a and a.patient_id == patient_id else None


@router.post("/allergies/{allergy_id}/edit", dependencies=_EDIT)
async def edit_allergy(request: Request, patient_id: int, allergy_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    form, p = await _guard(request, db, patient_id)
    a = _allergy(db, patient_id, allergy_id) if p else None
    if not a:
        return HTMLResponse("Not found", status_code=404)
    if not med_svc.update_allergy(db, a, user.id, form):
        return _back(patient_id, "An allergen is required.")
    db.commit()
    return _back(patient_id)


@router.post("/allergies/{allergy_id}/{action}", dependencies=_EDIT)
async def allergy_status(request: Request, patient_id: int, allergy_id: int, action: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    form, p = await _guard(request, db, patient_id)
    a = _allergy(db, patient_id, allergy_id) if p else None
    if not a or action not in ("deactivate", "reactivate"):
        return HTMLResponse("Not found", status_code=404)
    med_svc.set_allergy_status(db, a, "inactive" if action == "deactivate" else "active", user.id)
    db.commit()
    return _back(patient_id)


@router.post("/review", dependencies=_EDIT)
async def review(request: Request, patient_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    form, p = await _guard(request, db, patient_id)
    if not p:
        return HTMLResponse("Not found", status_code=404)
    if not med_svc.record_review(db, patient_id, form.get("kind", ""), form.get("outcome", ""), user.id):
        return _back(patient_id, "That review can't be recorded: a list with active items can't be marked 'none reported'.")
    db.commit()
    return _back(patient_id)

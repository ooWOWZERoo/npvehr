"""Patient-facing safety-flag routes (ROS plan stage 5): recording a patient's flags from the chart, and the
warnings feed read by the New Exam page. Both are patient-scoped and respect record-level authorization
(a provider only reaches their own patients; anything else is a 404, not a 403, so existence isn't confirmed)."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from ehr.auth import csrf
from ehr.auth.deps import get_current_user
from ehr.auth.permissions import PATIENT_SAFETY_EDIT, require_role
from ehr.models.database import Patient, get_db
from ehr.services import authz, safety

router = APIRouter(prefix="/safety", tags=["safety"])


def _patient_or_none(db: Session, user, patient_id: int):
    p = db.get(Patient, patient_id)
    return p if p and authz.can_view_patient(db, user, patient_id) else None


@router.get("/patients/{patient_id}/warnings.json")
def warnings_feed(request: Request, patient_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Eligible warnings for the exam form's banner: flag is yes, rule active and reviewed. Keyword matching against
    the exam text happens in the browser as the clinician types; the sign step re-evaluates on the server."""
    if not _patient_or_none(db, user, patient_id):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return JSONResponse({"warnings": safety.evaluate(db, patient_id, None)}, headers={"Cache-Control": "no-store"})


@router.post("/patients/{patient_id}/flags", dependencies=[Depends(require_role(*PATIENT_SAFETY_EDIT))])
async def save_flags(request: Request, patient_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    form = await request.form()
    csrf.verify_or_403(request.state.csrf_token, form.get("csrf_token"))
    if not _patient_or_none(db, user, patient_id):
        return HTMLResponse("Not found", status_code=404)
    for ftype, _current, _row in safety.flags_for_patient(db, patient_id):
        key = f"flag_{ftype.id}"
        if key in form:
            safety.set_flag(db, patient_id, ftype.id, form.get(key, ""), user.id)
    db.commit()
    return RedirectResponse(f"/patients/{patient_id}", status_code=303)

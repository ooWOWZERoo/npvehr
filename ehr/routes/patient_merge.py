"""Merge Patient (replaces the placeholder at /patients/merge): find likely duplicate charts, compare two side by side, choose
which chart to keep and how to combine the demographics, and merge the other into it -- with an audit event and an undo.
Administrators only. The compare page shows exactly what will move and what will be set aside before anything changes, and
the merge itself needs a reason and the word MERGE typed. See ehr/services/patient_merge.py for what a merge does."""
from typing import List
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ehr.auth import csrf
from ehr.auth.deps import get_current_user
from ehr.auth.permissions import PATIENT_MERGE, ROLE_LABELS, require_role
from ehr.env_info import EHR_ENV
from ehr.models.database import Patient, User, get_db
from ehr.models.patient_merge import PatientMergeEvent
from ehr.services import patient_merge as svc

router = APIRouter(prefix="/patients/merge", tags=["patient-merge"], dependencies=[Depends(require_role(*PATIENT_MERGE))])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS


def _label(p: Patient) -> str:
    return f"{p.last_name}, {p.first_name}"


@router.get("", response_class=HTMLResponse)
def index(request: Request, q: str = "", db: Session = Depends(get_db)):
    results = []
    if q.strip():
        like = f"%{q.strip()}%"
        results = (db.query(Patient).filter((Patient.last_name.ilike(like)) | (Patient.first_name.ilike(like)) | (Patient.phone.ilike(like))
                                            | (Patient.mrn.ilike(like))).order_by(Patient.last_name, Patient.first_name).limit(25).all())
    events = db.query(PatientMergeEvent).order_by(PatientMergeEvent.id.desc()).limit(20).all()
    users = {u.id: f"{u.first_name} {u.last_name}" for u in db.query(User).all()}
    return templates.TemplateResponse(request, "patients/merge_index.html",
        {"candidates": svc.duplicate_candidates(db), "q": q, "results": results, "events": events, "users": users,
         "error": request.query_params.get("error"), "done": request.query_params.get("done")})


@router.get("/compare", response_class=HTMLResponse)
def compare(request: Request, p: List[int] = Query([]), keep: int = 0, db: Session = Depends(get_db)):
    ids = list(dict.fromkeys(p))
    if len(ids) != 2:
        return RedirectResponse("/patients/merge?error=" + quote("Choose exactly two patients to compare."), status_code=303)
    a, b = db.get(Patient, ids[0]), db.get(Patient, ids[1])
    if not a or not b:
        return RedirectResponse("/patients/merge?error=" + quote("One of those patients no longer exists."), status_code=303)
    if keep not in ids:                                   # default: keep the chart with more linked records (then the older one)
        na, nb = sum(svc.counts(db, a.id).values()), sum(svc.counts(db, b.id).values())
        keep = a.id if (na, -a.id) >= (nb, -b.id) else b.id
    kp, dp = (a, b) if keep == a.id else (b, a)
    return templates.TemplateResponse(request, "patients/merge_compare.html",
        {"keep": kp, "dup": dp, "fields": svc.field_plan(kp, dp), "preview": svc.preview(db, kp.id, dp.id),
         "balance_note": (kp.balance_due or 0) != 0 or (dp.balance_due or 0) != 0,
         "optin_note": bool((kp.sms_opt_in and not dp.sms_opt_in) or (kp.email_opt_in and not dp.email_opt_in)),
         "error": request.query_params.get("error")})


@router.post("")
async def execute(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    form = await request.form()
    csrf.verify_or_403(request.state.csrf_token, form.get("csrf_token"))
    try:
        keep_id, dup_id = int(form.get("keep_id", "")), int(form.get("dup_id", ""))
    except ValueError:
        return RedirectResponse("/patients/merge?error=" + quote("Choose two patients to merge."), status_code=303)
    back = f"/patients/merge/compare?p={keep_id}&p={dup_id}&keep={keep_id}"
    if (form.get("confirm") or "").strip() != "MERGE":
        return RedirectResponse(back + "&error=" + quote("Type MERGE to confirm."), status_code=303)
    choices = {k[6:]: v for k, v in form.items() if k.startswith("field_") and v in ("keep", "dup", "both")}
    try:
        ev = svc.merge(db, keep_id, dup_id, user.id, form.get("reason", ""), choices)
        db.commit()
    except svc.MergeError as exc:
        db.rollback()
        return RedirectResponse(back + "&error=" + quote(str(exc)), status_code=303)
    except Exception:                                    # noqa: BLE001 -- nothing partial is kept
        db.rollback()
        return RedirectResponse(back + "&error=" + quote("The merge failed and nothing was changed."), status_code=303)
    return RedirectResponse(f"/patients/merge?done={ev.id}", status_code=303)


@router.post("/{event_id}/undo")
def undo(request: Request, event_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    ev = db.get(PatientMergeEvent, event_id)
    if not ev:
        return HTMLResponse("Not found", status_code=404)
    try:
        svc.undo(db, ev, user.id)
        db.commit()
    except svc.MergeError as exc:
        db.rollback()
        return RedirectResponse("/patients/merge?error=" + quote(str(exc)), status_code=303)
    except Exception:                                    # noqa: BLE001
        db.rollback()
        return RedirectResponse("/patients/merge?error=" + quote("The undo failed and nothing was changed."), status_code=303)
    return RedirectResponse("/patients/merge", status_code=303)

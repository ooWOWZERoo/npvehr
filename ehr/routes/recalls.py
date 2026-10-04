"""Recall worklist (patients due back): `/recalls`. Lists open recalls by urgency with context that helps decide whether
anyone needs calling, and lets front-desk staff record a contact attempt, close a recall as satisfied, dismiss it with a
reason, or reopen it. Informational and staff-driven: nothing here sends a message. Record-level authorization applies
(a provider only sees their own patients' recalls). See ehr/services/recalls.py."""
from datetime import date
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ehr.auth import csrf
from ehr.auth.deps import get_current_user
from ehr.auth.permissions import RECALL_VIEW, RECALL_WORK, ROLE_LABELS, require_role
from ehr.env_info import EHR_ENV
from ehr.models.database import get_db
from ehr.models.imports import PatientRecall
from ehr.services import authz
from ehr.services import pagination as pg
from ehr.services import recalls as svc

router = APIRouter(prefix="/recalls", tags=["recalls"])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS
PAGE_SIZE = 50


def _self_url(request: Request) -> str:
    """This page's own URL with its filters but without a stale ?error=, so an action returns to the same filtered view."""
    params = [(k, v) for k, v in request.query_params.multi_items() if k != "error"]
    return "/recalls" + (("?" + urlencode(params)) if params else "")


def _back(next_url: str, msg: str = ""):
    url = next_url if next_url.startswith("/recalls") and "//" not in next_url else "/recalls"
    if msg:
        url += ("&" if "?" in url else "?") + "error=" + quote(msg)
    return RedirectResponse(url, status_code=303)


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_role(*RECALL_VIEW))])
def worklist(request: Request, status: str = "open", window: str = "30", rtype: str = "", q: str = "", hide_booked: str = "",
             page: int = 1, db: Session = Depends(get_db)):
    user = request.state.user
    status = status if status in svc.STATUSES else "open"
    window = window if window in svc.WINDOWS else "30"
    query = svc.worklist_query(db, user, status, window, rtype, q, hide_booked == "1")
    rows, total, total_pages, page = pg.paginate(query, page, PAGE_SIZE)
    ctx = {"rows": rows, "ctx": svc.context_for(db, rows), "today": date.today(), "counts": svc.counts(db, user),
           "status": status, "window": window, "rtype": rtype, "q": q, "hide_booked": hide_booked == "1",
           "statuses": svc.STATUSES, "windows": svc.WINDOWS, "methods": svc.METHODS, "types": svc.recall_types(db),
           "can_work": user.role in RECALL_WORK, "error": request.query_params.get("error"),
           "self_url": _self_url(request)}
    ctx.update(pg.pagination_context(request, page, total, total_pages, PAGE_SIZE))
    return templates.TemplateResponse(request, "recalls/worklist.html", ctx)


def _recall(db, user, recall_id):
    r = db.get(PatientRecall, recall_id)
    return r if r and authz.can_view_patient(db, user, r.patient_id) else None


@router.post("/{recall_id}/contact", dependencies=[Depends(require_role(*RECALL_WORK))])
def contact(request: Request, recall_id: int, method: str = Form(""), note: str = Form(""), next: str = Form("/recalls"),
            csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    r = _recall(db, user, recall_id)
    if not r:
        return HTMLResponse("Not found", status_code=404)
    if not svc.log_contact(db, r, user.id, method, note):
        return _back(next, "Choose how the patient was contacted.")
    db.commit()
    return _back(next)


@router.post("/{recall_id}/close", dependencies=[Depends(require_role(*RECALL_WORK))])
def close(request: Request, recall_id: int, outcome: str = Form(""), note: str = Form(""), next: str = Form("/recalls"),
          csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    r = _recall(db, user, recall_id)
    if not r:
        return HTMLResponse("Not found", status_code=404)
    err = svc.close(db, r, outcome, user.id, note)
    if err:
        return _back(next, err)
    db.commit()
    return _back(next)


@router.post("/{recall_id}/reopen", dependencies=[Depends(require_role(*RECALL_WORK))])
def reopen(request: Request, recall_id: int, next: str = Form("/recalls"), csrf_token: str = Form(""),
           db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    r = _recall(db, user, recall_id)
    if not r:
        return HTMLResponse("Not found", status_code=404)
    svc.reopen(db, r, user.id)
    db.commit()
    return _back(next)

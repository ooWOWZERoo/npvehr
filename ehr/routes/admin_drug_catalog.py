"""Drug catalog administration: load the drug names behind the medication intake from NLM RxNorm (ehr/services/rxnorm.py)
and see what the catalog holds. Loading is an explicit administrator action with an audit row each time (who, when, how
many names, which RxNorm release); it only ever ADDS names, so existing and manually entered rows are never changed.
Only the fixed RxNorm list request leaves the app -- no patient data."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from ehr.auth import csrf
from ehr.auth.deps import get_current_user
from ehr.auth.permissions import DRUG_CATALOG_EDIT, DRUG_CATALOG_VIEW, ROLE_LABELS, require_role
from ehr.env_info import EHR_ENV
from ehr.models.database import User, get_db
from ehr.models.drug_catalog import DrugCatalogEntry, DrugCatalogLoadEvent, DrugDosage
from ehr.services import rxnorm

router = APIRouter(prefix="/admin/drug-catalog", tags=["admin-drug-catalog"])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_role(*DRUG_CATALOG_VIEW))])
def index(request: Request, db: Session = Depends(get_db)):
    by_source = dict(db.query(DrugCatalogEntry.source, func.count(DrugCatalogEntry.id)).group_by(DrugCatalogEntry.source).all())
    events = (db.query(DrugCatalogLoadEvent, User).outerjoin(User, User.id == DrugCatalogLoadEvent.user_id)
              .order_by(DrugCatalogLoadEvent.loaded_at.desc(), DrugCatalogLoadEvent.id.desc()).limit(10).all())
    return templates.TemplateResponse(request, "admin/drug_catalog/index.html", {
        "rxnorm_count": by_source.get("rxnorm", 0), "manual_count": by_source.get("manual", 0),
        "resolved_count": db.query(func.count(DrugCatalogEntry.id)).filter(DrugCatalogEntry.resolved_at.isnot(None)).scalar(),
        "dosage_count": db.query(func.count(DrugDosage.id)).scalar(), "events": events,
        "can_edit": request.state.user.role in DRUG_CATALOG_EDIT, "flash": request.query_params.get("flash"),
        "error": request.query_params.get("error")})


@router.post("/load", dependencies=[Depends(require_role(*DRUG_CATALOG_EDIT))])
def load(request: Request, csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    ev = rxnorm.load_display_names(db, user.id)
    db.commit()
    if ev.status == "ok":
        return RedirectResponse(f"/admin/drug-catalog?flash=Loaded {ev.names_added} new drug names ({ev.names_in_source} in RxNorm).", status_code=303)
    return RedirectResponse("/admin/drug-catalog?error=RxNorm could not be reached, so nothing was loaded. Try again later.", status_code=303)

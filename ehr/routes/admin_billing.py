"""Service & Fee Catalog administration (user request: "the ability to
create a service with associated cpt code"). Staff-facing pricing only --
this app has no billing/claims infrastructure, and nothing here is ever
transmitted or submitted as a real claim or invoice; see Service's own
docstring in ehr/models/database.py. Route/permission/audit shape mirrors
Provider Management (admin_scheduling.py) since fee is an editable field
needing a real edit path, not just a create+toggle flat list.
"""
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ehr.models.database import get_db, Service
from ehr.services import field_audit
from ehr.env_info import EHR_ENV
from ehr.auth.permissions import require_role, SERVICE_CATALOG_EDIT, ROLE_LABELS
from ehr.auth.deps import get_current_user
from ehr.auth import csrf

router = APIRouter(prefix="/admin/billing", tags=["admin-billing"])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS

SERVICE_AUDITED_FIELDS = ["code", "name", "category", "cpt_code", "fee"]


@router.get("/services", response_class=HTMLResponse, dependencies=[Depends(require_role(*SERVICE_CATALOG_EDIT))])
def list_services(request: Request, db: Session = Depends(get_db)):
    services = db.query(Service).order_by(Service.category, Service.display_order).all()
    return templates.TemplateResponse(request, "admin/billing/services_list.html", {"services": services})


@router.post("/services/new", dependencies=[Depends(require_role(*SERVICE_CATALOG_EDIT))])
def create_service(request: Request, code: str = Form(...), name: str = Form(...), category: str = Form(...),
    cpt_code: str = Form(""), fee: float = Form(...), csrf_token: str = Form(""), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    code = code.strip().upper()
    if db.query(Service).filter(Service.code == code).first():
        return HTMLResponse(f"Service code '{code}' already exists.", status_code=400)
    db.add(Service(code=code, name=name.strip(), category=category.strip(),
        cpt_code=cpt_code.strip() or None, fee=fee, active=True))
    db.commit()
    return RedirectResponse("/admin/billing/services", status_code=303)


@router.get("/services/{service_id}/edit", response_class=HTMLResponse,
    dependencies=[Depends(require_role(*SERVICE_CATALOG_EDIT))])
def edit_service_form(request: Request, service_id: int, db: Session = Depends(get_db)):
    service = db.query(Service).filter(Service.id == service_id).first()
    if not service:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "admin/billing/service_form.html", {"service": service, "error": None})


@router.post("/services/{service_id}/edit", dependencies=[Depends(require_role(*SERVICE_CATALOG_EDIT))])
def update_service(request: Request, service_id: int, code: str = Form(...), name: str = Form(...),
    category: str = Form(...), cpt_code: str = Form(""), fee: float = Form(...),
    csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    service = db.query(Service).filter(Service.id == service_id).first()
    if not service:
        return HTMLResponse("Not found", status_code=404)
    code = code.strip().upper()
    conflict = db.query(Service).filter(Service.code == code, Service.id != service_id).first()
    if conflict:
        return templates.TemplateResponse(request, "admin/billing/service_form.html",
            {"service": service, "error": f"Service code '{code}' is already used by another service."},
            status_code=400)
    before = {f: getattr(service, f) for f in SERVICE_AUDITED_FIELDS}
    service.code = code
    service.name = name.strip()
    service.category = category.strip()
    service.cpt_code = cpt_code.strip() or None
    service.fee = fee
    after = {f: getattr(service, f) for f in SERVICE_AUDITED_FIELDS}
    field_audit.record_field_changes(db, "services", service_id, before, after, user.id)
    db.commit()
    return RedirectResponse("/admin/billing/services", status_code=303)


@router.post("/services/{service_id}/toggle-active", dependencies=[Depends(require_role(*SERVICE_CATALOG_EDIT))])
def toggle_service_active(request: Request, service_id: int, csrf_token: str = Form(""),
    db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    service = db.query(Service).filter(Service.id == service_id).first()
    if not service:
        return HTMLResponse("Not found", status_code=404)
    before_active = service.active
    service.active = not service.active
    field_audit.record_field_changes(db, "services", service_id,
        {"active": before_active}, {"active": service.active}, user.id)
    db.commit()
    return RedirectResponse("/admin/billing/services", status_code=303)

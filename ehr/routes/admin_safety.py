"""Clinical safety-flag and warning-rule administration (ROS plan stage 5). Same shape as the ROS
catalog (admin_ros.py): audited, and a rule is silent until someone with SAFETY_RULES_REVIEW signs it
off WITH a source citation (reviewer and time recorded). Editing a reviewed rule's warning text, keyword
or flag withdraws the sign-off. The practice's clinicians define the rules; this screen ships none.

Flag types and rules are deactivated, never deleted: patient answers and exam acknowledgements point at
them (both FKs RESTRICT)."""
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ehr.auth import csrf
from ehr.auth.deps import get_current_user
from ehr.auth.permissions import (ROLE_LABELS, SAFETY_RULES_EDIT, SAFETY_RULES_REVIEW, SAFETY_RULES_VIEW,
                                  require_role)
from ehr.env_info import EHR_ENV
from ehr.models.database import get_db
from ehr.models.safety import SafetyFlagType, SafetyRule
from ehr.services import field_audit

router = APIRouter(prefix="/admin/safety", tags=["admin-safety"])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS

TYPE_AUDITED = ["label", "description", "sort_order", "is_active"]
RULE_AUDITED = ["flag_type_id", "warning_text", "keyword", "is_active", "source_citation", "reviewed"]


def _slug(label: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "_" for c in label.strip())
    return "_".join(p for p in out.split("_") if p)[:40]


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_role(*SAFETY_RULES_VIEW))])
def index(request: Request, db: Session = Depends(get_db)):
    types = db.query(SafetyFlagType).order_by(SafetyFlagType.sort_order, SafetyFlagType.id).all()
    rules = db.query(SafetyRule).order_by(SafetyRule.flag_type_id, SafetyRule.id).all()
    return templates.TemplateResponse(request, "admin/safety/index.html",
        {"types": types, "rules": rules, "error": request.query_params.get("error")})


@router.post("/flag-types/new", dependencies=[Depends(require_role(*SAFETY_RULES_EDIT))])
def create_type(request: Request, label: str = Form(...), description: str = Form(""), csrf_token: str = Form(""),
    db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    label = label.strip()
    code = _slug(label)
    if not label or not code or len(label) > 120:
        return RedirectResponse("/admin/safety?error=A label (up to 120 characters) is required.", status_code=303)
    if db.query(SafetyFlagType).filter(SafetyFlagType.code == code).first():
        return RedirectResponse(f"/admin/safety?error='{label}' already exists.", status_code=303)
    nxt = (db.query(SafetyFlagType).count() + 1) * 10
    t = SafetyFlagType(code=code, label=label, description=description.strip()[:255] or None, sort_order=nxt, is_active=True)
    db.add(t); db.flush()
    field_audit.record_field_changes(db, "safety_flag_types", t.id, {}, {"label": label, "is_active": True}, user.id)
    db.commit()
    return RedirectResponse("/admin/safety", status_code=303)


@router.get("/flag-types/{type_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_role(*SAFETY_RULES_EDIT))])
def edit_type_form(request: Request, type_id: int, db: Session = Depends(get_db)):
    t = db.get(SafetyFlagType, type_id)
    if not t:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "admin/safety/type_form.html", {"t": t, "error": None})


@router.post("/flag-types/{type_id}/edit", dependencies=[Depends(require_role(*SAFETY_RULES_EDIT))])
def update_type(request: Request, type_id: int, label: str = Form(...), description: str = Form(""), sort_order: int = Form(0),
    csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    t = db.get(SafetyFlagType, type_id)
    if not t:
        return HTMLResponse("Not found", status_code=404)
    label = label.strip()
    if not label or len(label) > 120:
        return templates.TemplateResponse(request, "admin/safety/type_form.html",
            {"t": t, "error": "A label (up to 120 characters) is required."}, status_code=400)
    before = {f: getattr(t, f) for f in TYPE_AUDITED}
    t.label, t.description, t.sort_order = label, description.strip()[:255] or None, sort_order
    field_audit.record_field_changes(db, "safety_flag_types", type_id, before, {f: getattr(t, f) for f in TYPE_AUDITED}, user.id)
    db.commit()
    return RedirectResponse("/admin/safety", status_code=303)


@router.post("/flag-types/{type_id}/toggle-active", dependencies=[Depends(require_role(*SAFETY_RULES_EDIT))])
def toggle_type(request: Request, type_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    t = db.get(SafetyFlagType, type_id)
    if not t:
        return HTMLResponse("Not found", status_code=404)
    before = t.is_active
    t.is_active = not t.is_active
    field_audit.record_field_changes(db, "safety_flag_types", type_id, {"is_active": before}, {"is_active": t.is_active}, user.id)
    db.commit()
    return RedirectResponse("/admin/safety", status_code=303)


@router.post("/rules/new", dependencies=[Depends(require_role(*SAFETY_RULES_EDIT))])
def create_rule(request: Request, flag_type_id: int = Form(...), warning_text: str = Form(...), keyword: str = Form(""),
    csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    warning_text, keyword = warning_text.strip(), keyword.strip()
    if not db.get(SafetyFlagType, flag_type_id):
        return RedirectResponse("/admin/safety?error=Choose a patient flag for the rule.", status_code=303)
    if not warning_text or len(warning_text) > 500 or len(keyword) > 80:
        return RedirectResponse("/admin/safety?error=A warning (up to 500 characters) is required; the keyword is up to 80.", status_code=303)
    r = SafetyRule(flag_type_id=flag_type_id, warning_text=warning_text, keyword=keyword or None, is_active=True, reviewed=False)
    db.add(r); db.flush()
    field_audit.record_field_changes(db, "safety_rules", r.id, {}, {"warning_text": warning_text, "reviewed": False}, user.id)
    db.commit()
    return RedirectResponse("/admin/safety", status_code=303)


@router.get("/rules/{rule_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_role(*SAFETY_RULES_EDIT))])
def edit_rule_form(request: Request, rule_id: int, db: Session = Depends(get_db)):
    r = db.get(SafetyRule, rule_id)
    if not r:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "admin/safety/rule_form.html",
        {"r": r, "types": db.query(SafetyFlagType).order_by(SafetyFlagType.sort_order).all(), "error": None})


@router.post("/rules/{rule_id}/edit", dependencies=[Depends(require_role(*SAFETY_RULES_EDIT))])
def update_rule(request: Request, rule_id: int, flag_type_id: int = Form(...), warning_text: str = Form(...), keyword: str = Form(""),
    csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    r = db.get(SafetyRule, rule_id)
    if not r:
        return HTMLResponse("Not found", status_code=404)
    warning_text, keyword = warning_text.strip(), keyword.strip() or None
    types = db.query(SafetyFlagType).order_by(SafetyFlagType.sort_order).all()
    if not db.get(SafetyFlagType, flag_type_id) or not warning_text or len(warning_text) > 500 or (keyword and len(keyword) > 80):
        return templates.TemplateResponse(request, "admin/safety/rule_form.html",
            {"r": r, "types": types, "error": "A flag and a warning (up to 500 characters) are required; the keyword is up to 80."}, status_code=400)
    before = {f: getattr(r, f) for f in RULE_AUDITED}
    changed = (flag_type_id, warning_text, keyword) != (r.flag_type_id, r.warning_text, r.keyword)
    r.flag_type_id, r.warning_text, r.keyword = flag_type_id, warning_text, keyword
    if changed and r.reviewed:
        r.reviewed, r.reviewed_by_user_id, r.reviewed_at = False, None, None   # the sign-off covered the old wording
    field_audit.record_field_changes(db, "safety_rules", rule_id, before, {f: getattr(r, f) for f in RULE_AUDITED}, user.id)
    db.commit()
    return RedirectResponse("/admin/safety", status_code=303)


@router.post("/rules/{rule_id}/toggle-active", dependencies=[Depends(require_role(*SAFETY_RULES_EDIT))])
def toggle_rule(request: Request, rule_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    r = db.get(SafetyRule, rule_id)
    if not r:
        return HTMLResponse("Not found", status_code=404)
    before = r.is_active
    r.is_active = not r.is_active
    field_audit.record_field_changes(db, "safety_rules", rule_id, {"is_active": before}, {"is_active": r.is_active}, user.id)
    db.commit()
    return RedirectResponse("/admin/safety", status_code=303)


@router.post("/rules/{rule_id}/review", dependencies=[Depends(require_role(*SAFETY_RULES_REVIEW))])
def review_rule(request: Request, rule_id: int, source_citation: str = Form(""), csrf_token: str = Form(""),
    db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    r = db.get(SafetyRule, rule_id)
    if not r:
        return HTMLResponse("Not found", status_code=404)
    cite = source_citation.strip()
    if not cite:
        return RedirectResponse("/admin/safety?error=A source citation (the guideline or policy this warning comes from) is required to mark a rule reviewed.", status_code=303)
    before = {f: getattr(r, f) for f in RULE_AUDITED}
    r.source_citation, r.reviewed, r.reviewed_by_user_id, r.reviewed_at = cite[:255], True, user.id, datetime.utcnow()
    field_audit.record_field_changes(db, "safety_rules", rule_id, before, {f: getattr(r, f) for f in RULE_AUDITED}, user.id)
    db.commit()
    return RedirectResponse("/admin/safety", status_code=303)


@router.post("/rules/{rule_id}/unreview", dependencies=[Depends(require_role(*SAFETY_RULES_REVIEW))])
def unreview_rule(request: Request, rule_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    r = db.get(SafetyRule, rule_id)
    if not r:
        return HTMLResponse("Not found", status_code=404)
    field_audit.record_field_changes(db, "safety_rules", rule_id, {"reviewed": True}, {"reviewed": False}, user.id)
    r.reviewed, r.reviewed_by_user_id, r.reviewed_at = False, None, None
    db.commit()
    return RedirectResponse("/admin/safety", status_code=303)

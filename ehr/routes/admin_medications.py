"""Drug-class administration for the medication list (see PATIENT_MEDICATION_LIST_DESIGN.md). The practice's
clinicians define the classes and the generic/brand terms in them; this screen ships none. A term classifies
nothing until someone with SAFETY_RULES_REVIEW signs it off WITH a source citation (reviewer and time recorded),
exactly like the ROS and safety rules. A class is linked to a patient safety flag so the flag can be derived from
a patient's medication list. Classes are deactivated, never deleted (flag links point at them); a term is a pure
lookup row with nothing downstream, so it can be deleted outright."""
import re
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ehr.auth import csrf
from ehr.auth.deps import get_current_user
from ehr.auth.permissions import (ROLE_LABELS, SAFETY_RULES_EDIT, SAFETY_RULES_REVIEW, SAFETY_RULES_VIEW, require_role)
from ehr.env_info import EHR_ENV
from ehr.models.database import get_db
from ehr.models.medications import MedicationClass, MedicationClassTerm, SafetyFlagClassLink
from ehr.models.safety import SafetyFlagType
from ehr.services import field_audit

router = APIRouter(prefix="/admin/medications", tags=["admin-medications"])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS
_EDIT = [Depends(require_role(*SAFETY_RULES_EDIT))]
_REVIEW = [Depends(require_role(*SAFETY_RULES_REVIEW))]
TERM_AUDITED = ["term", "source_citation", "reviewed"]


def _home(error=None):
    return RedirectResponse("/admin/medications" + (f"?error={error}" if error else ""), status_code=303)


def _slug(label: str) -> str:
    out = "".join(c.lower() if c.isalnum() else "_" for c in label.strip())
    return "_".join(p for p in out.split("_") if p)[:40]


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_role(*SAFETY_RULES_VIEW))])
def index(request: Request, db: Session = Depends(get_db)):
    classes = db.query(MedicationClass).order_by(MedicationClass.sort_order, MedicationClass.id).all()
    flags = db.query(SafetyFlagType).filter(SafetyFlagType.is_active.is_(True)).order_by(SafetyFlagType.sort_order, SafetyFlagType.id).all()
    links = {}
    for l in db.query(SafetyFlagClassLink).all():
        links.setdefault(l.class_id, []).append(l.flag_type_id)
    return templates.TemplateResponse(request, "admin/medications/index.html",
        {"classes": classes, "flags": flags, "flag_label": {f.id: f.label for f in db.query(SafetyFlagType).all()},
         "links": links, "error": request.query_params.get("error")})


@router.post("/classes/new", dependencies=_EDIT)
def create_class(request: Request, label: str = Form(...), description: str = Form(""), csrf_token: str = Form(""),
                 db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    label = label.strip()
    code = _slug(label)
    if not label or not code or len(label) > 120:
        return _home("A class name (up to 120 characters) is required.")
    if db.query(MedicationClass).filter(MedicationClass.code == code).first():
        return _home(f"'{label}' already exists.")
    c = MedicationClass(code=code, label=label, description=description.strip()[:255] or None,
                        sort_order=(db.query(MedicationClass).count() + 1) * 10, is_active=True)
    db.add(c)
    db.flush()
    field_audit.record_field_changes(db, "medication_classes", c.id, {}, {"label": label, "is_active": True}, user.id)
    db.commit()
    return _home()


@router.post("/classes/{class_id}/toggle-active", dependencies=_EDIT)
def toggle_class(request: Request, class_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    c = db.get(MedicationClass, class_id)
    if not c:
        return HTMLResponse("Not found", status_code=404)
    before = c.is_active
    c.is_active = not c.is_active
    field_audit.record_field_changes(db, "medication_classes", class_id, {"is_active": before}, {"is_active": c.is_active}, user.id)
    db.commit()
    return _home()


@router.post("/classes/{class_id}/terms/new", dependencies=_EDIT)
def add_term(request: Request, class_id: int, term: str = Form(...), csrf_token: str = Form(""),
             db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    if not db.get(MedicationClass, class_id):
        return HTMLResponse("Not found", status_code=404)
    added = 0
    for raw in re.split(r"[,\n;]", term):
        t = " ".join(raw.lower().split())
        if not t or len(t) > 80:
            continue
        if db.query(MedicationClassTerm).filter(MedicationClassTerm.class_id == class_id, MedicationClassTerm.term == t).first():
            continue
        row = MedicationClassTerm(class_id=class_id, term=t, reviewed=False)
        db.add(row)
        db.flush()
        field_audit.record_field_changes(db, "medication_class_terms", row.id, {}, {"term": t, "reviewed": False}, user.id)
        added += 1
    if not added:
        return _home("Enter at least one new drug name (up to 80 characters each); separate several with commas.")
    db.commit()
    return _home()


@router.post("/terms/{term_id}/review", dependencies=_REVIEW)
def review_term(request: Request, term_id: int, source_citation: str = Form(""), csrf_token: str = Form(""),
                db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    t = db.get(MedicationClassTerm, term_id)
    if not t:
        return HTMLResponse("Not found", status_code=404)
    cite = source_citation.strip()
    if not cite:
        return _home("A source citation (the formulary or guideline that puts this drug in the class) is required to mark a term reviewed.")
    before = {f: getattr(t, f) for f in TERM_AUDITED}
    t.source_citation, t.reviewed, t.reviewed_by_user_id, t.reviewed_at = cite[:255], True, user.id, datetime.utcnow()
    field_audit.record_field_changes(db, "medication_class_terms", term_id, before, {f: getattr(t, f) for f in TERM_AUDITED}, user.id)
    db.commit()
    return _home()


@router.post("/terms/{term_id}/unreview", dependencies=_REVIEW)
def unreview_term(request: Request, term_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    t = db.get(MedicationClassTerm, term_id)
    if not t:
        return HTMLResponse("Not found", status_code=404)
    field_audit.record_field_changes(db, "medication_class_terms", term_id, {"reviewed": True}, {"reviewed": False}, user.id)
    t.reviewed, t.reviewed_by_user_id, t.reviewed_at = False, None, None
    db.commit()
    return _home()


@router.post("/terms/{term_id}/delete", dependencies=_EDIT)
def delete_term(request: Request, term_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    t = db.get(MedicationClassTerm, term_id)
    if not t:
        return HTMLResponse("Not found", status_code=404)
    field_audit.record_field_changes(db, "medication_class_terms", term_id, {"term": t.term}, {"term": None}, user.id)
    db.delete(t)
    db.commit()
    return _home()


@router.post("/classes/{class_id}/links/new", dependencies=_EDIT)
def link_flag(request: Request, class_id: int, flag_type_id: int = Form(...), csrf_token: str = Form(""),
              db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    if not db.get(MedicationClass, class_id) or not db.get(SafetyFlagType, flag_type_id):
        return _home("Choose a patient flag to link.")
    if not db.query(SafetyFlagClassLink).filter_by(class_id=class_id, flag_type_id=flag_type_id).first():
        db.add(SafetyFlagClassLink(class_id=class_id, flag_type_id=flag_type_id))
        field_audit.record_field_changes(db, "safety_flag_class_links", class_id, {"flag_type_id": None}, {"flag_type_id": flag_type_id}, user.id)
        db.commit()
    return _home()


@router.post("/classes/{class_id}/links/{flag_type_id}/delete", dependencies=_EDIT)
def unlink_flag(request: Request, class_id: int, flag_type_id: int, csrf_token: str = Form(""),
                db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    link = db.query(SafetyFlagClassLink).filter_by(class_id=class_id, flag_type_id=flag_type_id).first()
    if link:
        field_audit.record_field_changes(db, "safety_flag_class_links", class_id, {"flag_type_id": flag_type_id}, {"flag_type_id": None}, user.id)
        db.delete(link)
        db.commit()
    return _home()

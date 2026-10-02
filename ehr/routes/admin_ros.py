"""Review-of-Systems catalog administration: the ROS prompts per body system and
the curated ROS -> (ICD-10, CPT) decision-support rules (tables in
ehr/models/ros.py). Route/permission/audit shape mirrors the other admin
catalogs (Service & Fee Catalog, Voice Scribe corrections): a flat list with
inline add, a dedicated edit page, toggle-active, audited via field_audit.

Compliance sign-off is explicit and accountable: a rule is `reviewed` only when
someone with ROS_CATALOG_REVIEW marks it so WITH a source citation (the same
"flag + required reason" shape as the app's override pattern), and the reviewer
and time are recorded. Editing a reviewed rule's codes or text withdraws the
sign-off, since the content changed after it was approved. Unreviewed rules are
suggestions only.

Prompts are retired by deactivating, never deleted: answers recorded against a
prompt (encounter_ros_responses) restrict its deletion. Mappings are pure rules
with nothing downstream, so they can be deleted outright.
"""
import re
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from ehr.auth import csrf
from ehr.auth.deps import get_current_user
from ehr.auth.permissions import (ROLE_LABELS, ROS_CATALOG_EDIT, ROS_CATALOG_REVIEW, ROS_CATALOG_VIEW,
                                  require_role)
from ehr.env_info import EHR_ENV
from ehr.models.database import get_db
from ehr.models.ros import ROS_SYSTEMS, RosIcd10TestMapping, RosMaster
from ehr.services import field_audit

router = APIRouter(prefix="/admin/ros", tags=["admin-ros"])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS

ITEM_AUDITED_FIELDS = ["body_system", "prompt_text", "sort_order", "is_active"]
MAPPING_AUDITED_FIELDS = ["suggested_icd10", "icd10_pattern", "recommended_cpt", "compliance_rule",
                          "source_citation", "reviewed"]
_ICD_RE = re.compile(r"^[A-TV-Z][0-9][0-9A-Z](\.[0-9A-Z]{1,4})?$")
_ICD_PATTERN_RE = re.compile(r"^[A-TV-Z][0-9][0-9A-Z](\.[0-9A-Z]{0,4})?$")
_CPT_RE = re.compile(r"^[0-9]{4}[0-9A-Z]$")


def _clean_mapping_fields(icd, pattern, cpt):
    """Normalises and validates the three code fields; returns (icd, pattern, cpt, error)."""
    icd = (icd or "").strip().upper()
    pattern = (pattern or "").strip().upper() or None
    cpt = (cpt or "").strip().upper()
    if not _ICD_RE.match(icd):
        return icd, pattern, cpt, f"'{icd}' is not a valid ICD-10-CM code shape (e.g. E11.9)."
    if pattern and not _ICD_PATTERN_RE.match(pattern):
        return icd, pattern, cpt, f"'{pattern}' is not a valid ICD-10 family prefix (e.g. E11 or E11.3)."
    if not _CPT_RE.match(cpt):
        return icd, pattern, cpt, f"'{cpt}' is not a valid 5-character CPT/HCPCS code (e.g. 92250)."
    return icd, pattern, cpt, None


def _render_item_form(request, item, error=None, status_code=200):
    return templates.TemplateResponse(request, "admin/ros/item_form.html",
        {"item": item, "systems": ROS_SYSTEMS, "error": error}, status_code=status_code)


def _render_mapping_form(request, mapping, error=None, status_code=200):
    return templates.TemplateResponse(request, "admin/ros/mapping_form.html",
        {"m": mapping, "error": error}, status_code=status_code)


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_role(*ROS_CATALOG_VIEW))])
def list_items(request: Request, db: Session = Depends(get_db)):
    items = db.query(RosMaster).order_by(RosMaster.body_system, RosMaster.sort_order, RosMaster.id).all()
    counts = dict(db.query(RosIcd10TestMapping.ros_item_id, func.count(RosIcd10TestMapping.id))
                  .group_by(RosIcd10TestMapping.ros_item_id).all())
    reviewed_counts = dict(db.query(RosIcd10TestMapping.ros_item_id, func.count(RosIcd10TestMapping.id))
                           .filter(RosIcd10TestMapping.reviewed.is_(True))
                           .group_by(RosIcd10TestMapping.ros_item_id).all())
    grouped = {}
    for it in items:
        grouped.setdefault(it.body_system, []).append(it)
    ordered = [(s, grouped.pop(s)) for s in ROS_SYSTEMS if s in grouped] + sorted(grouped.items())
    return templates.TemplateResponse(request, "admin/ros/items_list.html",
        {"grouped": ordered, "counts": counts, "reviewed_counts": reviewed_counts, "systems": ROS_SYSTEMS})


@router.post("/items/new", dependencies=[Depends(require_role(*ROS_CATALOG_EDIT))])
def create_item(request: Request, body_system: str = Form(...), prompt_text: str = Form(...),
    csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    prompt_text = prompt_text.strip()
    if body_system not in ROS_SYSTEMS or not prompt_text or len(prompt_text) > 255:
        return HTMLResponse("A body system and a prompt (up to 255 characters) are required.", status_code=400)
    if db.query(RosMaster).filter(RosMaster.body_system == body_system, RosMaster.prompt_text == prompt_text).first():
        return HTMLResponse(f"'{prompt_text}' already exists under {body_system}.", status_code=400)
    next_order = (db.query(func.max(RosMaster.sort_order)).filter(RosMaster.body_system == body_system).scalar() or 0) + 10
    item = RosMaster(body_system=body_system, prompt_text=prompt_text, sort_order=next_order, is_active=True)
    db.add(item)
    db.flush()
    field_audit.record_field_changes(db, "ros_master", item.id, {}, {"prompt_text": prompt_text, "is_active": True}, user.id)
    db.commit()
    return RedirectResponse("/admin/ros", status_code=303)


@router.get("/items/{item_id}", response_class=HTMLResponse, dependencies=[Depends(require_role(*ROS_CATALOG_VIEW))])
def item_detail(request: Request, item_id: int, db: Session = Depends(get_db)):
    item = db.query(RosMaster).filter(RosMaster.id == item_id).first()
    if not item:
        return HTMLResponse("Not found", status_code=404)
    mappings = (db.query(RosIcd10TestMapping).filter(RosIcd10TestMapping.ros_item_id == item_id)
                .order_by(RosIcd10TestMapping.recommended_cpt, RosIcd10TestMapping.suggested_icd10).all())
    return templates.TemplateResponse(request, "admin/ros/item_detail.html",
        {"item": item, "mappings": mappings, "error": request.query_params.get("error")})


@router.get("/items/{item_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_role(*ROS_CATALOG_EDIT))])
def edit_item_form(request: Request, item_id: int, db: Session = Depends(get_db)):
    item = db.query(RosMaster).filter(RosMaster.id == item_id).first()
    if not item:
        return HTMLResponse("Not found", status_code=404)
    return _render_item_form(request, item)


@router.post("/items/{item_id}/edit", dependencies=[Depends(require_role(*ROS_CATALOG_EDIT))])
def update_item(request: Request, item_id: int, body_system: str = Form(...), prompt_text: str = Form(...),
    sort_order: int = Form(0), csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    item = db.query(RosMaster).filter(RosMaster.id == item_id).first()
    if not item:
        return HTMLResponse("Not found", status_code=404)
    prompt_text = prompt_text.strip()
    if body_system not in ROS_SYSTEMS or not prompt_text or len(prompt_text) > 255:
        return _render_item_form(request, item, "A body system and a prompt (up to 255 characters) are required.", 400)
    clash = db.query(RosMaster).filter(RosMaster.body_system == body_system, RosMaster.prompt_text == prompt_text,
                                       RosMaster.id != item_id).first()
    if clash:
        return _render_item_form(request, item, f"'{prompt_text}' already exists under {body_system}.", 400)
    before = {f: getattr(item, f) for f in ITEM_AUDITED_FIELDS}
    item.body_system, item.prompt_text, item.sort_order = body_system, prompt_text, sort_order
    after = {f: getattr(item, f) for f in ITEM_AUDITED_FIELDS}
    field_audit.record_field_changes(db, "ros_master", item_id, before, after, user.id)
    db.commit()
    return RedirectResponse("/admin/ros", status_code=303)


@router.post("/items/{item_id}/toggle-active", dependencies=[Depends(require_role(*ROS_CATALOG_EDIT))])
def toggle_item_active(request: Request, item_id: int, csrf_token: str = Form(""),
    db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    item = db.query(RosMaster).filter(RosMaster.id == item_id).first()
    if not item:
        return HTMLResponse("Not found", status_code=404)
    before_active = item.is_active
    item.is_active = not item.is_active
    field_audit.record_field_changes(db, "ros_master", item_id, {"is_active": before_active}, {"is_active": item.is_active}, user.id)
    db.commit()
    return RedirectResponse("/admin/ros", status_code=303)


@router.post("/items/{item_id}/mappings/new", dependencies=[Depends(require_role(*ROS_CATALOG_EDIT))])
def create_mapping(request: Request, item_id: int, suggested_icd10: str = Form(...), icd10_pattern: str = Form(""),
    recommended_cpt: str = Form(...), compliance_rule: str = Form(""), csrf_token: str = Form(""),
    db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    item = db.query(RosMaster).filter(RosMaster.id == item_id).first()
    if not item:
        return HTMLResponse("Not found", status_code=404)
    icd, pattern, cpt, error = _clean_mapping_fields(suggested_icd10, icd10_pattern, recommended_cpt)
    if not error and db.query(RosIcd10TestMapping).filter(RosIcd10TestMapping.ros_item_id == item_id,
            RosIcd10TestMapping.suggested_icd10 == icd, RosIcd10TestMapping.recommended_cpt == cpt).first():
        error = f"A rule for {icd} / {cpt} already exists on this prompt."
    if error:
        return RedirectResponse(f"/admin/ros/items/{item_id}?error={error}", status_code=303)
    m = RosIcd10TestMapping(ros_item_id=item_id, suggested_icd10=icd, icd10_pattern=pattern, recommended_cpt=cpt,
                            compliance_rule=compliance_rule.strip() or None, reviewed=False)
    db.add(m)
    db.flush()
    field_audit.record_field_changes(db, "ros_icd10_test_mapping", m.id, {},
        {"suggested_icd10": icd, "recommended_cpt": cpt, "reviewed": False}, user.id)
    db.commit()
    return RedirectResponse(f"/admin/ros/items/{item_id}", status_code=303)


@router.get("/mappings/{mapping_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_role(*ROS_CATALOG_EDIT))])
def edit_mapping_form(request: Request, mapping_id: int, db: Session = Depends(get_db)):
    m = db.query(RosIcd10TestMapping).filter(RosIcd10TestMapping.id == mapping_id).first()
    if not m:
        return HTMLResponse("Not found", status_code=404)
    return _render_mapping_form(request, m)


@router.post("/mappings/{mapping_id}/edit", dependencies=[Depends(require_role(*ROS_CATALOG_EDIT))])
def update_mapping(request: Request, mapping_id: int, suggested_icd10: str = Form(...), icd10_pattern: str = Form(""),
    recommended_cpt: str = Form(...), compliance_rule: str = Form(""), csrf_token: str = Form(""),
    db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    m = db.query(RosIcd10TestMapping).filter(RosIcd10TestMapping.id == mapping_id).first()
    if not m:
        return HTMLResponse("Not found", status_code=404)
    icd, pattern, cpt, error = _clean_mapping_fields(suggested_icd10, icd10_pattern, recommended_cpt)
    if not error and db.query(RosIcd10TestMapping).filter(RosIcd10TestMapping.ros_item_id == m.ros_item_id,
            RosIcd10TestMapping.suggested_icd10 == icd, RosIcd10TestMapping.recommended_cpt == cpt,
            RosIcd10TestMapping.id != mapping_id).first():
        error = f"A rule for {icd} / {cpt} already exists on this prompt."
    if error:
        return _render_mapping_form(request, m, error, 400)
    rule = compliance_rule.strip() or None
    before = {f: getattr(m, f) for f in MAPPING_AUDITED_FIELDS}
    content_changed = (icd, pattern, cpt, rule) != (m.suggested_icd10, m.icd10_pattern, m.recommended_cpt, m.compliance_rule)
    m.suggested_icd10, m.icd10_pattern, m.recommended_cpt, m.compliance_rule = icd, pattern, cpt, rule
    if content_changed and m.reviewed:
        m.reviewed, m.reviewed_by_user_id, m.reviewed_at = False, None, None   # sign-off covered the old content
    after = {f: getattr(m, f) for f in MAPPING_AUDITED_FIELDS}
    field_audit.record_field_changes(db, "ros_icd10_test_mapping", mapping_id, before, after, user.id)
    db.commit()
    return RedirectResponse(f"/admin/ros/items/{m.ros_item_id}", status_code=303)


@router.post("/mappings/{mapping_id}/review", dependencies=[Depends(require_role(*ROS_CATALOG_REVIEW))])
def review_mapping(request: Request, mapping_id: int, source_citation: str = Form(""), csrf_token: str = Form(""),
    db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    m = db.query(RosIcd10TestMapping).filter(RosIcd10TestMapping.id == mapping_id).first()
    if not m:
        return HTMLResponse("Not found", status_code=404)
    citation = source_citation.strip()
    if not citation:
        return RedirectResponse(f"/admin/ros/items/{m.ros_item_id}?error=A source citation (the LCD or policy this rule comes from) is required to mark a rule reviewed.", status_code=303)
    before = {f: getattr(m, f) for f in MAPPING_AUDITED_FIELDS}
    m.source_citation, m.reviewed, m.reviewed_by_user_id, m.reviewed_at = citation[:255], True, user.id, datetime.utcnow()
    after = {f: getattr(m, f) for f in MAPPING_AUDITED_FIELDS}
    field_audit.record_field_changes(db, "ros_icd10_test_mapping", mapping_id, before, after, user.id)
    db.commit()
    return RedirectResponse(f"/admin/ros/items/{m.ros_item_id}", status_code=303)


@router.post("/mappings/{mapping_id}/unreview", dependencies=[Depends(require_role(*ROS_CATALOG_REVIEW))])
def unreview_mapping(request: Request, mapping_id: int, csrf_token: str = Form(""),
    db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    m = db.query(RosIcd10TestMapping).filter(RosIcd10TestMapping.id == mapping_id).first()
    if not m:
        return HTMLResponse("Not found", status_code=404)
    field_audit.record_field_changes(db, "ros_icd10_test_mapping", mapping_id, {"reviewed": True}, {"reviewed": False}, user.id)
    m.reviewed, m.reviewed_by_user_id, m.reviewed_at = False, None, None
    db.commit()
    return RedirectResponse(f"/admin/ros/items/{m.ros_item_id}", status_code=303)


@router.post("/mappings/{mapping_id}/delete", dependencies=[Depends(require_role(*ROS_CATALOG_EDIT))])
def delete_mapping(request: Request, mapping_id: int, csrf_token: str = Form(""),
    db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    m = db.query(RosIcd10TestMapping).filter(RosIcd10TestMapping.id == mapping_id).first()
    if not m:
        return HTMLResponse("Not found", status_code=404)
    item_id = m.ros_item_id
    field_audit.record_field_changes(db, "ros_icd10_test_mapping", mapping_id,
        {"suggested_icd10": m.suggested_icd10, "recommended_cpt": m.recommended_cpt}, {"suggested_icd10": None, "recommended_cpt": None}, user.id)
    db.delete(m)
    db.commit()
    return RedirectResponse(f"/admin/ros/items/{item_id}", status_code=303)

"""Drug catalog administration: load the drug names behind the medication intake from NLM RxNorm (ehr/services/rxnorm.py),
browse and search the catalog, add drugs by hand, adjust any drug's route, dosages and brand names, and edit the frequency
pick list. Loading is an explicit administrator action with an audit row each time (who, when, how many names, which RxNorm
release); it only ever ADDS names, so existing and manually entered rows are never changed. Edits go to the field-change
audit trail. Drugs and frequencies are deactivated, never deleted (patient records may point at them). Only the fixed RxNorm
list request leaves the app -- no patient data."""
from urllib.parse import quote

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
from ehr.models.drug_catalog import DrugAlias, DrugCatalogEntry, DrugCatalogLoadEvent, DrugDosage, DrugFrequency
from ehr.services import drug_catalog as catalog
from ehr.services import field_audit, rxnorm
from ehr.services import pagination as pg

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


# ------------------------------------------------------------------------------------------------ browse / add drugs
_EDIT = [Depends(require_role(*DRUG_CATALOG_EDIT))]
_VIEW = [Depends(require_role(*DRUG_CATALOG_VIEW))]
ROUTE_CHOICES = [("", "Not specified / other"), ("ophthalmic", "Ophthalmic (eye)"), ("oral", "Oral"), ("topical", "Topical (skin)")]
DRUG_AUDITED = ["name", "default_route", "is_active"]


def _go(path, flash=None, error=None):
    q = (f"flash={quote(flash)}" if flash else "") + (f"error={quote(error)}" if error else "")
    return RedirectResponse(path + ("?" + q if q else ""), status_code=303)


def _route_value(group: str):
    return group if group in ("ophthalmic", "oral", "topical") else None


def _lines(text: str, maxlen: int) -> list:
    seen, out = set(), []
    for raw in (text or "").splitlines():
        t = raw.strip()[:maxlen]
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


@router.get("/drugs", response_class=HTMLResponse, dependencies=_VIEW)
def drugs(request: Request, q: str = "", source: str = "", page: int = 1, db: Session = Depends(get_db)):
    query = db.query(DrugCatalogEntry)
    q = q.strip()
    if q:
        like = "%" + catalog._like_escape(q.lower()) + "%"
        query = query.filter(DrugCatalogEntry.name_lower.like(like, escape="\\"))
    if source in ("rxnorm", "manual"):
        query = query.filter(DrugCatalogEntry.source == source)
    query = query.order_by(DrugCatalogEntry.name_lower)
    rows, total, total_pages, page = pg.paginate(query, page, 50)
    counts = dict(db.query(DrugDosage.drug_id, func.count(DrugDosage.id)).filter(DrugDosage.drug_id.in_([d.id for d in rows] or [0]))
                  .group_by(DrugDosage.drug_id).all()) if rows else {}
    ctx = {"drugs": rows, "dose_counts": counts, "q": q, "source": source, "route_choices": ROUTE_CHOICES,
           "can_edit": request.state.user.role in DRUG_CATALOG_EDIT, "flash": request.query_params.get("flash"),
           "error": request.query_params.get("error"), "route_group": catalog.route_group}
    ctx.update(pg.pagination_context(request, page, total, total_pages, 50))
    return templates.TemplateResponse(request, "admin/drug_catalog/drugs.html", ctx)


@router.post("/drugs/new", dependencies=_EDIT)
def add_drug(request: Request, name: str = Form(""), route: str = Form(""), dosages: str = Form(""), aliases: str = Form(""),
             csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    name = name.strip()[:160]
    if not name:
        return _go("/admin/drug-catalog/drugs", error="A drug name is required.")
    existing = db.query(DrugCatalogEntry).filter(DrugCatalogEntry.name_lower == name.lower()).first()
    if existing:
        return _go(f"/admin/drug-catalog/drugs/{existing.id}", error=f"\u201c{existing.name}\u201d is already in the catalog \u2014 edit it here instead.")
    d = DrugCatalogEntry(name=name, name_lower=name.lower(), source="manual", default_route=_route_value(route), is_active=True)
    db.add(d)
    db.flush()
    for label in _lines(dosages, 160):
        db.add(DrugDosage(drug_id=d.id, label=label))
    for a in _lines(aliases, 160):
        if a.lower() != d.name_lower:
            db.add(DrugAlias(drug_id=d.id, alias=a, alias_lower=a.lower(), source="manual"))
    field_audit.record_field_changes(db, "drug_catalog", d.id, {}, {"name": name, "default_route": d.default_route, "is_active": True}, user.id)
    db.commit()
    return _go(f"/admin/drug-catalog/drugs/{d.id}", flash=f"Added {name}.")


def _drug(db, drug_id):
    return db.get(DrugCatalogEntry, drug_id)


@router.get("/drugs/{drug_id}", response_class=HTMLResponse, dependencies=_VIEW)
def drug_detail(request: Request, drug_id: int, db: Session = Depends(get_db)):
    d = _drug(db, drug_id)
    if not d:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "admin/drug_catalog/drug.html", {
        "drug": d, "dosages": catalog.dosages_for(db, d),
        "aliases": db.query(DrugAlias).filter(DrugAlias.drug_id == d.id).order_by(DrugAlias.alias_lower).all(),
        "route_choices": ROUTE_CHOICES, "group": catalog.route_group(d.default_route),
        "can_edit": request.state.user.role in DRUG_CATALOG_EDIT, "flash": request.query_params.get("flash"),
        "error": request.query_params.get("error")})


@router.post("/drugs/{drug_id}/edit", dependencies=_EDIT)
def edit_drug(request: Request, drug_id: int, name: str = Form(""), route: str = Form(""), csrf_token: str = Form(""),
              db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    d = _drug(db, drug_id)
    if not d:
        return HTMLResponse("Not found", status_code=404)
    before = {f: getattr(d, f) for f in DRUG_AUDITED}
    if d.source == "manual":                                         # an RxNorm name is RxNorm's to define; only hand-added names can be renamed
        new_name = name.strip()[:160]
        if not new_name:
            return _go(f"/admin/drug-catalog/drugs/{d.id}", error="A drug name is required.")
        clash = db.query(DrugCatalogEntry.id).filter(DrugCatalogEntry.name_lower == new_name.lower(), DrugCatalogEntry.id != d.id).first()
        if clash:
            return _go(f"/admin/drug-catalog/drugs/{d.id}", error="Another drug already has that name.")
        d.name, d.name_lower = new_name, new_name.lower()
    if catalog.route_group(d.default_route) != (route or None):       # only touch the route if the choice actually changed
        d.default_route = _route_value(route)
    field_audit.record_field_changes(db, "drug_catalog", d.id, before, {f: getattr(d, f) for f in DRUG_AUDITED}, user.id)
    db.commit()
    return _go(f"/admin/drug-catalog/drugs/{d.id}", flash="Saved.")


@router.post("/drugs/{drug_id}/toggle-active", dependencies=_EDIT)
def toggle_drug(request: Request, drug_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    d = _drug(db, drug_id)
    if not d:
        return HTMLResponse("Not found", status_code=404)
    before = {"is_active": d.is_active}
    d.is_active = not d.is_active
    field_audit.record_field_changes(db, "drug_catalog", d.id, before, {"is_active": d.is_active}, user.id)
    db.commit()
    return _go(f"/admin/drug-catalog/drugs/{d.id}", flash="Reactivated." if d.is_active else "Deactivated \u2014 it no longer appears in searches.")


@router.post("/drugs/{drug_id}/dosages/new", dependencies=_EDIT)
def add_dosages(request: Request, drug_id: int, dosages: str = Form(""), csrf_token: str = Form(""), db: Session = Depends(get_db),
                user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    d = _drug(db, drug_id)
    if not d:
        return HTMLResponse("Not found", status_code=404)
    have = {x.label.lower() for x in catalog.dosages_for(db, d)}
    added = [label for label in _lines(dosages, 160) if label.lower() not in have]
    for label in added:
        db.add(DrugDosage(drug_id=d.id, label=label))
        field_audit.record_field_changes(db, "drug_dosages", d.id, {"label": None}, {"label": label}, user.id)
    db.commit()
    return _go(f"/admin/drug-catalog/drugs/{d.id}", flash=f"Added {len(added)} dosage(s)." if added else None,
               error=None if added else "Nothing new to add.")


@router.post("/drugs/{drug_id}/dosages/{dosage_id}/delete", dependencies=_EDIT)
def delete_dosage(request: Request, drug_id: int, dosage_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db),
                  user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    x = db.get(DrugDosage, dosage_id)
    if not x or x.drug_id != drug_id:
        return HTMLResponse("Not found", status_code=404)
    field_audit.record_field_changes(db, "drug_dosages", drug_id, {"label": x.label}, {"label": None}, user.id)
    db.delete(x)                      # a patient's medication keeps its own copy of the text; its dosage link just becomes NULL
    db.commit()
    return _go(f"/admin/drug-catalog/drugs/{drug_id}", flash="Dosage removed.")


@router.post("/drugs/{drug_id}/aliases/new", dependencies=_EDIT)
def add_aliases(request: Request, drug_id: int, aliases: str = Form(""), csrf_token: str = Form(""), db: Session = Depends(get_db),
                user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    d = _drug(db, drug_id)
    if not d:
        return HTMLResponse("Not found", status_code=404)
    n = 0
    for a in _lines(aliases, 160):
        if a.lower() != d.name_lower and not db.query(DrugAlias.id).filter(DrugAlias.drug_id == d.id, DrugAlias.alias_lower == a.lower()).first():
            db.add(DrugAlias(drug_id=d.id, alias=a, alias_lower=a.lower(), source="manual"))
            field_audit.record_field_changes(db, "drug_aliases", d.id, {"alias": None}, {"alias": a}, user.id)
            n += 1
    db.commit()
    return _go(f"/admin/drug-catalog/drugs/{d.id}", flash=f"Added {n} brand name(s)." if n else None, error=None if n else "Nothing new to add.")


@router.post("/drugs/{drug_id}/aliases/{alias_id}/delete", dependencies=_EDIT)
def delete_alias(request: Request, drug_id: int, alias_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db),
                 user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    a = db.get(DrugAlias, alias_id)
    if not a or a.drug_id != drug_id:
        return HTMLResponse("Not found", status_code=404)
    field_audit.record_field_changes(db, "drug_aliases", drug_id, {"alias": a.alias}, {"alias": None}, user.id)
    db.delete(a)
    db.commit()
    return _go(f"/admin/drug-catalog/drugs/{drug_id}", flash="Brand name removed.")


# ------------------------------------------------------------------------------------------------ frequency pick list
FREQ_GROUPS = [("ophthalmic", "Eye drops / ointments"), ("oral", "Oral"), ("topical", "Topical (skin)"), ("any", "Any route")]
FREQ_AUDITED = ["route_group", "label", "sort_order", "is_active"]


@router.get("/frequencies", response_class=HTMLResponse, dependencies=_VIEW)
def frequencies(request: Request, db: Session = Depends(get_db)):
    rows = db.query(DrugFrequency).order_by(DrugFrequency.sort_order, DrugFrequency.label).all()
    return templates.TemplateResponse(request, "admin/drug_catalog/frequencies.html", {
        "groups": [(code, label, [f for f in rows if f.route_group == code]) for code, label in FREQ_GROUPS], "group_choices": FREQ_GROUPS,
        "can_edit": request.state.user.role in DRUG_CATALOG_EDIT, "flash": request.query_params.get("flash"), "error": request.query_params.get("error")})


@router.post("/frequencies/new", dependencies=_EDIT)
def add_frequency(request: Request, route_group: str = Form(""), label: str = Form(""), csrf_token: str = Form(""),
                  db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    label = label.strip()[:80]
    if route_group not in dict(FREQ_GROUPS) or not label:
        return _go("/admin/drug-catalog/frequencies", error="Choose a group and type the wording.")
    if db.query(DrugFrequency.id).filter(DrugFrequency.route_group == route_group, func.lower(DrugFrequency.label) == label.lower()).first():
        return _go("/admin/drug-catalog/frequencies", error="That wording is already in this group.")
    top = db.query(func.max(DrugFrequency.sort_order)).filter(DrugFrequency.route_group == route_group).scalar() or 0
    f = DrugFrequency(route_group=route_group, label=label, sort_order=top + 1, is_active=True)
    db.add(f)
    db.flush()
    field_audit.record_field_changes(db, "drug_frequencies", f.id, {}, {k: getattr(f, k) for k in FREQ_AUDITED}, user.id)
    db.commit()
    return _go("/admin/drug-catalog/frequencies", flash=f"Added \u201c{label}\u201d.")


@router.post("/frequencies/{freq_id}/edit", dependencies=_EDIT)
def edit_frequency(request: Request, freq_id: int, label: str = Form(""), sort_order: int = Form(0), csrf_token: str = Form(""),
                   db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    f = db.get(DrugFrequency, freq_id)
    if not f:
        return HTMLResponse("Not found", status_code=404)
    label = label.strip()[:80]
    if not label:
        return _go("/admin/drug-catalog/frequencies", error="The wording can't be empty.")
    if db.query(DrugFrequency.id).filter(DrugFrequency.route_group == f.route_group, func.lower(DrugFrequency.label) == label.lower(),
                                         DrugFrequency.id != f.id).first():
        return _go("/admin/drug-catalog/frequencies", error="That wording is already in this group.")
    before = {k: getattr(f, k) for k in FREQ_AUDITED}
    f.label, f.sort_order = label, sort_order
    field_audit.record_field_changes(db, "drug_frequencies", f.id, before, {k: getattr(f, k) for k in FREQ_AUDITED}, user.id)
    db.commit()
    return _go("/admin/drug-catalog/frequencies", flash="Saved.")


@router.post("/frequencies/{freq_id}/toggle-active", dependencies=_EDIT)
def toggle_frequency(request: Request, freq_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    f = db.get(DrugFrequency, freq_id)
    if not f:
        return HTMLResponse("Not found", status_code=404)
    before = {"is_active": f.is_active}
    f.is_active = not f.is_active
    field_audit.record_field_changes(db, "drug_frequencies", f.id, before, {"is_active": f.is_active}, user.id)
    db.commit()
    return _go("/admin/drug-catalog/frequencies", flash="Reactivated." if f.is_active else "Hidden from the dropdown.")

"""Diabetic-retinopathy PCP communication: the outside-practitioner directory, a patient's primary-care link, the letter
(draft -> printable letter -> marked sent) and documented exclusions, plus the admin edit of the letter wording.
Staff-facing decision support only: nothing is transmitted; 'sent' is a staff attestation of how it left the office.
Everything is patient-scoped through record-level authorization (a provider only reaches their own patients; others get
a 404) and audited via field_audit. See ehr/services/pcp_letter.py for the trigger rule and what a valid letter needs."""
import re
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ehr.auth import csrf
from ehr.auth.deps import get_current_user
from ehr.auth.permissions import (COMM_TEMPLATE_EDIT, EXAM_VIEW, PCP_LETTER_EDIT, PCP_LETTER_EXCLUDE, ROLE_LABELS, require_role)
from ehr.env_info import EHR_ENV
from ehr.models.care_coordination import OutsidePractitioner, PatientPrimaryCare, PcpCommunication
from ehr.models.database import EyeExam, Patient, get_db
from ehr.services import authz, field_audit
from ehr.services import pcp_letter as svc

router = APIRouter(tags=["care-coordination"])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS
templates.env.globals["SEND_METHODS"] = svc.SEND_METHODS
templates.env.globals["SEVERITY_LABEL"] = svc.SEVERITY_LABEL
templates.env.globals["EDEMA_LABEL"] = svc.EDEMA_LABEL
_EDIT = [Depends(require_role(*PCP_LETTER_EDIT))]
_VIEW = [Depends(require_role(*EXAM_VIEW))]
PRACTITIONER_AUDITED = ["first_name", "last_name", "credentials", "practice_name", "npi", "phone", "fax", "direct_address",
                        "street_line_1", "street_line_2", "city", "state", "postal_code", "is_active"]


def _exam_for(db, user, exam_id):
    e = db.get(EyeExam, exam_id)
    return e if e and authz.can_view_patient(db, user, e.patient_id) else None


def _comm_for(db, user, comm_id):
    c = db.get(PcpCommunication, comm_id)
    return c if c and authz.can_view_patient(db, user, c.patient_id) else None


def _s(v, n):
    v = (v or "").strip()
    return v[:n] or None


# ------------------------------------------------------------------------------------------ directory
def _practitioner_from_form(form, p=None):
    """Returns (values dict, error). Fax is reduced to 10 digits; an NPI must be 10 digits."""
    first, last = _s(form.get("first_name"), 80), _s(form.get("last_name"), 80)
    if not first or not last:
        return None, "First and last name are required."
    fax = svc.clean_fax(form.get("fax"))
    npi = _s(re.sub(r"\D", "", form.get("npi") or ""), 10) if form.get("npi") else None
    if not svc.valid_fax(fax):
        return None, "A fax number needs 10 digits (area code first)."
    if not svc.valid_npi(npi):
        return None, "An NPI is exactly 10 digits."
    state = (form.get("state") or "").strip().upper()[:2] or None
    return {"first_name": first, "last_name": last, "credentials": _s(form.get("credentials"), 40), "practice_name": _s(form.get("practice_name"), 160),
            "npi": npi, "phone": _s(form.get("phone"), 20), "fax": fax, "direct_address": _s(form.get("direct_address"), 160),
            "street_line_1": _s(form.get("street_line_1"), 120), "street_line_2": _s(form.get("street_line_2"), 120),
            "city": _s(form.get("city"), 80), "state": state, "postal_code": _s(form.get("postal_code"), 10)}, None


@router.get("/care-providers", response_class=HTMLResponse, dependencies=_VIEW)
def directory(request: Request, q: str = "", db: Session = Depends(get_db)):
    qry = db.query(OutsidePractitioner)
    if q.strip():
        like = f"%{q.strip()}%"
        qry = qry.filter(or_(OutsidePractitioner.last_name.ilike(like), OutsidePractitioner.first_name.ilike(like),
                             OutsidePractitioner.practice_name.ilike(like)))
    rows = qry.order_by(OutsidePractitioner.last_name, OutsidePractitioner.first_name).limit(200).all()
    return templates.TemplateResponse(request, "care/directory.html",
        {"rows": rows, "q": q, "error": request.query_params.get("error"), "patient_id": request.query_params.get("patient_id"),
         "can_edit": request.state.user.role in PCP_LETTER_EDIT})


@router.post("/care-providers/new", dependencies=_EDIT)
async def directory_add(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    form = await request.form()
    csrf.verify_or_403(request.state.csrf_token, form.get("csrf_token"))
    values, error = _practitioner_from_form(form)
    pid = form.get("patient_id") or ""
    back = f"/care-providers?patient_id={pid}" if pid.isdigit() else "/care-providers"
    if error:
        return RedirectResponse(f"{back}{'&' if '?' in back else '?'}error={error}", status_code=303)
    row = OutsidePractitioner(created_by_user_id=user.id, is_active=True, **values)
    db.add(row)
    db.flush()
    field_audit.record_field_changes(db, "outside_practitioners", row.id, {}, {"first_name": row.first_name, "last_name": row.last_name, "is_active": True}, user.id)
    db.commit()
    if pid.isdigit() and authz.can_view_patient(db, user, int(pid)):
        return _assign(db, user, int(pid), row.id)
    return RedirectResponse(back, status_code=303)


@router.get("/care-providers/{pid}/edit", response_class=HTMLResponse, dependencies=_EDIT)
def directory_edit_form(request: Request, pid: int, db: Session = Depends(get_db)):
    row = db.get(OutsidePractitioner, pid)
    if not row:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "care/practitioner_form.html", {"p": row, "error": request.query_params.get("error")})


@router.post("/care-providers/{pid}/edit", dependencies=_EDIT)
async def directory_edit(request: Request, pid: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    form = await request.form()
    csrf.verify_or_403(request.state.csrf_token, form.get("csrf_token"))
    row = db.get(OutsidePractitioner, pid)
    if not row:
        return HTMLResponse("Not found", status_code=404)
    values, error = _practitioner_from_form(form, row)
    if error:
        return RedirectResponse(f"/care-providers/{pid}/edit?error={error}", status_code=303)
    before = {f: getattr(row, f) for f in PRACTITIONER_AUDITED}
    for k, v in values.items():
        setattr(row, k, v)
    field_audit.record_field_changes(db, "outside_practitioners", pid, before, {f: getattr(row, f) for f in PRACTITIONER_AUDITED}, user.id)
    db.commit()
    return RedirectResponse("/care-providers", status_code=303)


@router.post("/care-providers/{pid}/toggle-active", dependencies=_EDIT)
def directory_toggle(request: Request, pid: int, csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    row = db.get(OutsidePractitioner, pid)
    if not row:
        return HTMLResponse("Not found", status_code=404)
    before = row.is_active
    row.is_active = not row.is_active
    field_audit.record_field_changes(db, "outside_practitioners", pid, {"is_active": before}, {"is_active": row.is_active}, user.id)
    db.commit()
    return RedirectResponse("/care-providers", status_code=303)


# ------------------------------------------------------------------------------------------ patient's PCP
def _assign(db, user, patient_id, practitioner_id):
    row = db.query(PatientPrimaryCare).filter(PatientPrimaryCare.patient_id == patient_id).first()
    old = row.practitioner_id if row else None
    if row is None:
        db.add(PatientPrimaryCare(patient_id=patient_id, practitioner_id=practitioner_id, set_by_user_id=user.id))
    else:
        row.practitioner_id, row.set_by_user_id, row.set_at = practitioner_id, user.id, datetime.utcnow()
    field_audit.record_field_changes(db, "patient_primary_care", patient_id, {"practitioner_id": old}, {"practitioner_id": practitioner_id}, user.id)
    db.commit()
    return RedirectResponse(f"/patients/{patient_id}/primary-care", status_code=303)


@router.get("/patients/{patient_id}/primary-care", response_class=HTMLResponse, dependencies=_VIEW)
def primary_care(request: Request, patient_id: int, db: Session = Depends(get_db)):
    p = db.get(Patient, patient_id)
    if not p or not authz.can_view_patient(db, request.state.user, patient_id):
        return HTMLResponse("Not found", status_code=404)
    link = db.query(PatientPrimaryCare).filter(PatientPrimaryCare.patient_id == patient_id).first()
    return templates.TemplateResponse(request, "care/primary_care.html",
        {"patient": p, "current": link.practitioner if link else None, "can_edit": request.state.user.role in PCP_LETTER_EDIT,
         "back": request.query_params.get("back")})


@router.post("/patients/{patient_id}/primary-care/assign", dependencies=_EDIT)
def assign(request: Request, patient_id: int, practitioner_id: int = Form(...), csrf_token: str = Form(""),
           db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    pr = db.get(OutsidePractitioner, practitioner_id)
    if not db.get(Patient, patient_id) or not authz.can_view_patient(db, user, patient_id) or not pr or not pr.is_active:
        return HTMLResponse("Not found", status_code=404)
    return _assign(db, user, patient_id, practitioner_id)


@router.post("/patients/{patient_id}/primary-care/clear", dependencies=_EDIT)
def clear(request: Request, patient_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    if not db.get(Patient, patient_id) or not authz.can_view_patient(db, user, patient_id):
        return HTMLResponse("Not found", status_code=404)
    row = db.query(PatientPrimaryCare).filter(PatientPrimaryCare.patient_id == patient_id).first()
    if row:
        field_audit.record_field_changes(db, "patient_primary_care", patient_id, {"practitioner_id": row.practitioner_id}, {"practitioner_id": None}, user.id)
        db.delete(row)
        db.commit()
    return RedirectResponse(f"/patients/{patient_id}/primary-care", status_code=303)


# ------------------------------------------------------------------------------------------ the letter
@router.get("/exams/{exam_id}/pcp-letter", response_class=HTMLResponse, dependencies=_EDIT)
def letter_form(request: Request, exam_id: int, db: Session = Depends(get_db)):
    e = _exam_for(db, request.state.user, exam_id)
    if not e:
        return HTMLResponse("Not found", status_code=404)
    tpl = svc.get_template(db)
    return templates.TemplateResponse(request, "care/letter_form.html",
        {"exam": e, "sug": svc.suggestion(db, e), "pcp": svc.primary_care_for(db, e.patient_id), "template_ok": bool(tpl and not svc.template_error(tpl.body)),
         "severities": svc.SEVERITIES, "error": request.query_params.get("error"),
         "prior": db.query(PcpCommunication).filter(PcpCommunication.patient_id == e.patient_id).order_by(PcpCommunication.id.desc()).limit(5).all()})


@router.post("/exams/{exam_id}/pcp-letter", dependencies=_EDIT)
async def letter_create(request: Request, exam_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    form = await request.form()
    csrf.verify_or_403(request.state.csrf_token, form.get("csrf_token"))
    e = _exam_for(db, user, exam_id)
    if not e:
        return HTMLResponse("Not found", status_code=404)
    bad = lambda msg: RedirectResponse(f"/exams/{exam_id}/pcp-letter?error={msg}", status_code=303)   # noqa: E731
    severity, edema = form.get("severity"), form.get("macular_edema")
    if severity not in svc.SEVERITIES or edema not in svc.EDEMA_LABEL:
        return bad("Choose the retinopathy severity and whether macular edema is present or absent: the letter must say both.")
    if form.get("dilated") != "on":
        return bad("Confirm that a dilated macular or fundus exam was performed.")
    pcp = svc.primary_care_for(db, e.patient_id)
    if not pcp:
        return bad("Choose the patient's primary-care provider first.")
    tpl = svc.get_template(db)
    if not tpl or svc.template_error(tpl.body):
        return bad("The letter template is missing or invalid; an administrator can fix it under Communication Templates.")
    body = svc.render(tpl.body, {
        "pcp_name": ("Dr. " + svc.practitioner_label(pcp)) if pcp else "Colleague", "patient_name": f"{e.patient.first_name} {e.patient.last_name}",
        "dob": e.patient.date_of_birth or "not on file", "exam_date": e.exam_date, "severity": svc.SEVERITY_LABEL[severity],
        "macular_edema": svc.EDEMA_LABEL[edema], "findings": (form.get("findings") or "").strip()[:2000],
        "plan": (form.get("plan") or "").strip()[:2000] or "as discussed at the visit",
        "provider_name": f"Dr. {e.provider.first_name} {e.provider.last_name}"})
    c = PcpCommunication(patient_id=e.patient_id, exam_id=e.id, status="drafted", practitioner_id=pcp.id if pcp else None,
                         recipient_snapshot=svc.recipient_snapshot(pcp), severity=severity, macular_edema=edema,
                         dilated_exam_confirmed=True, body_snapshot=body, created_by_user_id=user.id)
    db.add(c)
    db.flush()
    field_audit.record_field_changes(db, "pcp_communications", c.id, {}, {"status": "drafted", "severity": severity, "macular_edema": edema}, user.id)
    db.commit()
    return RedirectResponse(f"/pcp-letters/{c.id}", status_code=303)


@router.get("/pcp-letters/{comm_id}", response_class=HTMLResponse, dependencies=_VIEW)
def letter_view(request: Request, comm_id: int, db: Session = Depends(get_db)):
    c = _comm_for(db, request.state.user, comm_id)
    if not c:
        return HTMLResponse("Not found", status_code=404)
    e = db.get(EyeExam, c.exam_id)
    return templates.TemplateResponse(request, "care/letter_view.html",
        {"c": c, "exam": e, "patient": db.get(Patient, c.patient_id), "can_edit": request.state.user.role in PCP_LETTER_EDIT,
         "error": request.query_params.get("error")})


@router.post("/pcp-letters/{comm_id}/sent", dependencies=_EDIT)
def letter_sent(request: Request, comm_id: int, method: str = Form(""), csrf_token: str = Form(""),
                db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    c = _comm_for(db, user, comm_id)
    if not c:
        return HTMLResponse("Not found", status_code=404)
    if c.status != "drafted":
        return RedirectResponse(f"/pcp-letters/{comm_id}", status_code=303)
    if method not in svc.SEND_METHODS:
        return RedirectResponse(f"/pcp-letters/{comm_id}?error=Choose how the letter was sent.", status_code=303)
    c.status, c.sent_method, c.sent_by_user_id, c.sent_at = "sent", method, user.id, datetime.utcnow()
    field_audit.record_field_changes(db, "pcp_communications", comm_id, {"status": "drafted"}, {"status": "sent", "sent_method": method}, user.id)
    db.commit()
    return RedirectResponse(f"/pcp-letters/{comm_id}", status_code=303)


@router.post("/exams/{exam_id}/pcp-letter/exclude", dependencies=[Depends(require_role(*PCP_LETTER_EXCLUDE))])
def letter_exclude(request: Request, exam_id: int, reason: str = Form(""), note: str = Form(""), csrf_token: str = Form(""),
                   db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    e = _exam_for(db, user, exam_id)
    if not e:
        return HTMLResponse("Not found", status_code=404)
    note = note.strip()
    if reason not in svc.EXCLUSIONS or (reason == "other" and len(note) < 5):
        return RedirectResponse(f"/exams/{exam_id}?pcp_error=Choose a reason for not sending a letter (and explain 'Other').", status_code=303)
    c = PcpCommunication(patient_id=e.patient_id, exam_id=e.id, status="excluded", exclusion_reason=reason, exclusion_note=note[:255] or None,
                         created_by_user_id=user.id)
    db.add(c)
    db.flush()
    field_audit.record_field_changes(db, "pcp_communications", c.id, {}, {"status": "excluded", "exclusion_reason": reason}, user.id)
    db.commit()
    return RedirectResponse(f"/exams/{exam_id}", status_code=303)


# ------------------------------------------------------------------------------------------ admin: wording
@router.get("/admin/communication-templates", response_class=HTMLResponse, dependencies=[Depends(require_role(*COMM_TEMPLATE_EDIT))])
def template_form(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "admin/communication_templates.html",
        {"tpl": svc.get_template(db), "tokens": svc.TOKENS, "error": request.query_params.get("error"), "saved": request.query_params.get("saved")})


@router.post("/admin/communication-templates", dependencies=[Depends(require_role(*COMM_TEMPLATE_EDIT))])
def template_save(request: Request, body: str = Form(...), csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    tpl = svc.get_template(db)
    if not tpl:
        return HTMLResponse("Not found", status_code=404)
    body = body.replace("\r\n", "\n").strip()
    err = svc.template_error(body)
    if err or len(body) > 6000:
        return RedirectResponse(f"/admin/communication-templates?error={err or 'The letter is limited to 6,000 characters.'}", status_code=303)
    before = tpl.body
    tpl.body, tpl.updated_by_user_id, tpl.updated_at = body, user.id, datetime.utcnow()
    field_audit.record_field_changes(db, "communication_templates", tpl.id, {"body": before}, {"body": body}, user.id)
    db.commit()
    return RedirectResponse("/admin/communication-templates?saved=1", status_code=303)

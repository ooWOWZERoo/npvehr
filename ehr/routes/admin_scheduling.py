from datetime import datetime
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from ehr.models.database import (get_db, AppointmentType, AppointmentTypeVersion, AppointmentTypeColorRule,
    DiagnosticTest, Resource, AvailabilityTemplate, AppointmentTypeAuditEvent, AppointmentAuditEvent, Appointment,
    PracticeClosure, Provider, ProviderAvailabilityTemplate)
from ehr.services import scheduling as sched
from ehr.env_info import EHR_ENV
from ehr.auth.permissions import require_role, ADMIN_SCHEDULING_VIEW, ADMIN_SCHEDULING_EDIT, ROLE_LABELS
from ehr.auth import csrf

router = APIRouter(prefix="/admin/scheduling", tags=["admin-scheduling"])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS

# Every route below now requires either ADMIN_SCHEDULING_VIEW (System/Practice
# Administrator, Read-only/Auditor) or ADMIN_SCHEDULING_EDIT (System/Practice
# Administrator only) via require_role(), applied per-route as a decorator
# dependency below -- see ehr/auth/permissions.py for the group definitions.


def _latest_version(type_: AppointmentType):
    return max(type_.versions, key=lambda v: v.version_number) if type_.versions else None


@router.get("/appointment-types", response_class=HTMLResponse, dependencies=[Depends(require_role(*ADMIN_SCHEDULING_VIEW))])
def list_types(request: Request, db: Session = Depends(get_db)):
    types = db.query(AppointmentType).order_by(AppointmentType.id).all()
    rows = [(t, _latest_version(t)) for t in types]
    rows.sort(key=lambda r: (r[1].display_order if r[1] else 9999))
    return templates.TemplateResponse(request, "admin/scheduling/types_list.html", {"rows": rows})


@router.get("/appointment-types/new", response_class=HTMLResponse, dependencies=[Depends(require_role(*ADMIN_SCHEDULING_EDIT))])
def new_type_form(request: Request):
    return templates.TemplateResponse(request, "admin/scheduling/type_form.html",
        {"type_": None, "version": None})


@router.post("/appointment-types/new", dependencies=[Depends(require_role(*ADMIN_SCHEDULING_EDIT))])
def create_type(request: Request, code: str = Form(...), internal_name: str = Form(...), display_name: str = Form(...),
    calendar_abbreviation: str = Form(...), description: str = Form(""), service_line: str = Form(...),
    display_order: int = Form(0), allows_new: bool = Form(False), allows_established: bool = Form(False),
    new_duration_minutes: str = Form(""), established_duration_minutes: str = Form(""),
    buffer_before_minutes: int = Form(0), buffer_after_minutes: int = Form(0),
    arrival_lead_minutes: int = Form(0), base_color: str = Form(""), active: bool = Form(False),
    csrf_token: str = Form(""), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    code = code.strip().upper()
    if db.query(AppointmentType).filter(AppointmentType.code == code).first():
        return HTMLResponse(f"Appointment type code '{code}' already exists.", status_code=400)
    if not allows_new and not allows_established:
        return HTMLResponse("At least one of new/established eligibility must be allowed.", status_code=400)
    t = AppointmentType(code=code, is_system_seeded=False, active=True)
    db.add(t); db.flush()
    v = AppointmentTypeVersion(appointment_type_id=t.id, version_number=1, internal_name=internal_name,
        display_name=display_name, calendar_abbreviation=calendar_abbreviation, description=description,
        service_line=service_line, display_order=display_order, allows_new=allows_new,
        allows_established=allows_established,
        new_duration_minutes=int(new_duration_minutes) if new_duration_minutes else None,
        established_duration_minutes=int(established_duration_minutes) if established_duration_minutes else None,
        buffer_before_minutes=buffer_before_minutes, buffer_after_minutes=buffer_after_minutes,
        arrival_lead_minutes=arrival_lead_minutes, base_color=base_color or None, staff_bookable=True,
        effective_from=datetime.utcnow().date().isoformat(), active=bool(active and base_color),
        change_reason="Created via admin UI")
    db.add(v); db.flush()
    db.add(AppointmentTypeAuditEvent(appointment_type_id=t.id, appointment_type_version_id=v.id,
        event_type="created", change_reason="Type created"))
    db.commit()
    return RedirectResponse(f"/admin/scheduling/appointment-types/{t.id}", status_code=303)


@router.get("/appointment-types/{type_id}", response_class=HTMLResponse, dependencies=[Depends(require_role(*ADMIN_SCHEDULING_VIEW))])
def type_detail(request: Request, type_id: int, db: Session = Depends(get_db)):
    t = db.query(AppointmentType).filter(AppointmentType.id == type_id).first()
    if not t: return HTMLResponse("Not found", status_code=404)
    v = _latest_version(t)
    previews = []
    if v:
        for rel in ("new", "established"):
            if not sched.eligible_for_relationship(v, rel):
                continue
            for fu in ([False, True] if v.appointment_type.code == "MED_EYE_EVAL" else [False]):
                for count in (0, 1, 2, 3):
                    color, reason = sched.resolve_color(v, rel, fu, count)
                    previews.append({"relationship": rel, "is_follow_up": fu, "count": count,
                                      "color": color, "reason": reason})
    return templates.TemplateResponse(request, "admin/scheduling/type_detail.html",
        {"type_": t, "version": v, "previews": previews})


@router.get("/appointment-types/{type_id}/color-rules", response_class=HTMLResponse, dependencies=[Depends(require_role(*ADMIN_SCHEDULING_VIEW))])
def color_rules(request: Request, type_id: int, db: Session = Depends(get_db)):
    """Edit the conditional color rules (spec 9.3/9.5) on a type's latest
    version through a real form, instead of direct database access -- the
    read-only Color Preview table on type_detail.html (driven by the same
    sched.resolve_color this page's rules feed) shows what these resolve to.
    Rules are ordered by priority (lowest wins first), matching
    AppointmentTypeVersion.color_rules' own relationship ordering."""
    t = db.query(AppointmentType).filter(AppointmentType.id == type_id).first()
    if not t: return HTMLResponse("Not found", status_code=404)
    v = _latest_version(t)
    return templates.TemplateResponse(request, "admin/scheduling/color_rules.html",
        {"type_": t, "version": v})


@router.post("/appointment-types/{type_id}/color-rules", dependencies=[Depends(require_role(*ADMIN_SCHEDULING_EDIT))])
def create_color_rule(request: Request, type_id: int, priority: int = Form(0), patient_relationship: str = Form(""),
    is_follow_up: str = Form(""), minimum_countable_tests: str = Form(""), maximum_countable_tests: str = Form(""),
    color: str = Form(...), reason_code: str = Form(""), csrf_token: str = Form(""), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    t = db.query(AppointmentType).filter(AppointmentType.id == type_id).first()
    if not t: return HTMLResponse("Not found", status_code=404)
    v = _latest_version(t)
    if not v: return HTMLResponse("This type has no version to attach a color rule to.", status_code=400)
    if not color.strip():
        return HTMLResponse("Color is required.", status_code=400)
    db.add(AppointmentTypeColorRule(
        appointment_type_version_id=v.id, priority=priority,
        patient_relationship=patient_relationship or None,  # '' means "any" (wildcard)
        is_follow_up={"": None, "yes": True, "no": False}.get(is_follow_up, None),
        minimum_countable_tests=int(minimum_countable_tests) if minimum_countable_tests else None,
        maximum_countable_tests=int(maximum_countable_tests) if maximum_countable_tests else None,
        color=color.strip(), reason_code=reason_code or None))
    db.add(AppointmentTypeAuditEvent(appointment_type_id=t.id, appointment_type_version_id=v.id,
        event_type="color_rule_added", change_reason=f"priority {priority}, color {color.strip()}"))
    db.commit()
    return RedirectResponse(f"/admin/scheduling/appointment-types/{type_id}/color-rules", status_code=303)


@router.post("/appointment-types/{type_id}/color-rules/{rule_id}/delete", dependencies=[Depends(require_role(*ADMIN_SCHEDULING_EDIT))])
def delete_color_rule(request: Request, type_id: int, rule_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    t = db.query(AppointmentType).filter(AppointmentType.id == type_id).first()
    if not t: return HTMLResponse("Not found", status_code=404)
    v = _latest_version(t)
    if not v: return HTMLResponse("Not found", status_code=404)
    rule = db.query(AppointmentTypeColorRule).filter(AppointmentTypeColorRule.id == rule_id,
        AppointmentTypeColorRule.appointment_type_version_id == v.id).first()
    if not rule: return HTMLResponse("Not found", status_code=404)
    db.delete(rule)
    db.add(AppointmentTypeAuditEvent(appointment_type_id=t.id, appointment_type_version_id=v.id,
        event_type="color_rule_deleted", change_reason=f"priority {rule.priority}, color {rule.color}"))
    db.commit()
    return RedirectResponse(f"/admin/scheduling/appointment-types/{type_id}/color-rules", status_code=303)


@router.get("/appointment-types/{type_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_role(*ADMIN_SCHEDULING_EDIT))])
def edit_type_form(request: Request, type_id: int, db: Session = Depends(get_db)):
    t = db.query(AppointmentType).filter(AppointmentType.id == type_id).first()
    if not t: return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "admin/scheduling/type_form.html",
        {"type_": t, "version": _latest_version(t)})


@router.post("/appointment-types/{type_id}/versions", dependencies=[Depends(require_role(*ADMIN_SCHEDULING_EDIT))])
def publish_new_version(request: Request, type_id: int, internal_name: str = Form(...), display_name: str = Form(...),
    calendar_abbreviation: str = Form(...), description: str = Form(""), service_line: str = Form(...),
    display_order: int = Form(0), allows_new: bool = Form(False), allows_established: bool = Form(False),
    new_duration_minutes: str = Form(""), established_duration_minutes: str = Form(""),
    buffer_before_minutes: int = Form(0), buffer_after_minutes: int = Form(0),
    arrival_lead_minutes: int = Form(0), base_color: str = Form(""), active: bool = Form(False),
    change_reason: str = Form(...), csrf_token: str = Form(""), db: Session = Depends(get_db)):
    """Publish an immutable new AppointmentTypeVersion (spec 8.3, 17.4). Existing
    appointments keep referencing their original version_id -- nothing here rewrites
    previously booked appointments (spec 3.22-3.24, 17.4)."""
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    t = db.query(AppointmentType).filter(AppointmentType.id == type_id).first()
    if not t: return HTMLResponse("Not found", status_code=404)
    if not change_reason:
        return HTMLResponse("A change reason is required to publish a new version.", status_code=400)
    if not allows_new and not allows_established:
        return HTMLResponse("At least one of new/established eligibility must be allowed.", status_code=400)
    prev = _latest_version(t)
    next_num = (prev.version_number + 1) if prev else 1
    v = AppointmentTypeVersion(appointment_type_id=t.id, version_number=next_num, internal_name=internal_name,
        display_name=display_name, calendar_abbreviation=calendar_abbreviation, description=description,
        service_line=service_line, display_order=display_order, allows_new=allows_new,
        allows_established=allows_established,
        new_duration_minutes=int(new_duration_minutes) if new_duration_minutes else None,
        established_duration_minutes=int(established_duration_minutes) if established_duration_minutes else None,
        buffer_before_minutes=buffer_before_minutes, buffer_after_minutes=buffer_after_minutes,
        arrival_lead_minutes=arrival_lead_minutes, base_color=base_color or None, staff_bookable=True,
        effective_from=datetime.utcnow().date().isoformat(), active=bool(active and base_color),
        change_reason=change_reason)
    if prev:
        # Carry forward color rules onto the new version (still editable afterward via direct DB access;
        # a dedicated color-rule editor UI is one of the deferred simplifications -- see report).
        for r in prev.color_rules:
            db.add(AppointmentTypeColorRule(appointment_type_version_id=None, priority=r.priority,
                patient_relationship=r.patient_relationship, is_follow_up=r.is_follow_up,
                minimum_countable_tests=r.minimum_countable_tests, maximum_countable_tests=r.maximum_countable_tests,
                color=r.color, reason_code=r.reason_code, version=v))
        prev.active = False
    db.add(v); db.flush()
    db.add(AppointmentTypeAuditEvent(appointment_type_id=t.id, appointment_type_version_id=v.id,
        event_type="version_published", change_reason=change_reason))
    db.commit()
    return RedirectResponse(f"/admin/scheduling/appointment-types/{type_id}", status_code=303)


@router.post("/appointment-types/{type_id}/activate", dependencies=[Depends(require_role(*ADMIN_SCHEDULING_EDIT))])
def activate_type(request: Request, type_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    t = db.query(AppointmentType).filter(AppointmentType.id == type_id).first()
    if not t: return HTMLResponse("Not found", status_code=404)
    v = _latest_version(t)
    if v and not v.base_color:
        return HTMLResponse("Cannot activate: assign a base color first (spec 9.2).", status_code=400)
    if v: v.active = True
    t.active = True
    db.add(AppointmentTypeAuditEvent(appointment_type_id=t.id, appointment_type_version_id=v.id if v else None,
        event_type="activated"))
    db.commit()
    return RedirectResponse(f"/admin/scheduling/appointment-types/{type_id}", status_code=303)


@router.post("/appointment-types/{type_id}/deactivate", dependencies=[Depends(require_role(*ADMIN_SCHEDULING_EDIT))])
def deactivate_type(request: Request, type_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    t = db.query(AppointmentType).filter(AppointmentType.id == type_id).first()
    if not t: return HTMLResponse("Not found", status_code=404)
    v = _latest_version(t)
    if v: v.active = False
    t.active = False
    db.add(AppointmentTypeAuditEvent(appointment_type_id=t.id, appointment_type_version_id=v.id if v else None,
        event_type="deactivated"))
    db.commit()
    return RedirectResponse(f"/admin/scheduling/appointment-types/{type_id}", status_code=303)


@router.post("/appointment-types/{type_id}/clone", dependencies=[Depends(require_role(*ADMIN_SCHEDULING_EDIT))])
def clone_type(request: Request, type_id: int, new_code: str = Form(...), csrf_token: str = Form(""), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    t = db.query(AppointmentType).filter(AppointmentType.id == type_id).first()
    if not t: return HTMLResponse("Not found", status_code=404)
    new_code = new_code.strip().upper()
    if db.query(AppointmentType).filter(AppointmentType.code == new_code).first():
        return HTMLResponse(f"Code '{new_code}' already exists.", status_code=400)
    v = _latest_version(t)
    nt = AppointmentType(code=new_code, is_system_seeded=False, active=False)
    db.add(nt); db.flush()
    nv = AppointmentTypeVersion(appointment_type_id=nt.id, version_number=1, internal_name=v.internal_name + " (Clone)",
        display_name=v.display_name + " (Clone)", calendar_abbreviation=v.calendar_abbreviation,
        description=v.description, service_line=v.service_line, display_order=v.display_order,
        allows_new=v.allows_new, allows_established=v.allows_established,
        new_duration_minutes=v.new_duration_minutes, established_duration_minutes=v.established_duration_minutes,
        buffer_before_minutes=v.buffer_before_minutes, buffer_after_minutes=v.buffer_after_minutes,
        arrival_lead_minutes=v.arrival_lead_minutes, base_color=v.base_color, staff_bookable=True,
        effective_from=datetime.utcnow().date().isoformat(), active=False, change_reason=f"Cloned from {t.code}")
    db.add(nv); db.flush()
    for r in v.color_rules:
        db.add(AppointmentTypeColorRule(priority=r.priority, patient_relationship=r.patient_relationship,
            is_follow_up=r.is_follow_up, minimum_countable_tests=r.minimum_countable_tests,
            maximum_countable_tests=r.maximum_countable_tests, color=r.color, reason_code=r.reason_code, version=nv))
    db.add(AppointmentTypeAuditEvent(appointment_type_id=nt.id, appointment_type_version_id=nv.id,
        event_type="cloned", change_reason=f"Cloned from {t.code} (#{t.id})"))
    db.commit()
    return RedirectResponse(f"/admin/scheduling/appointment-types/{nt.id}", status_code=303)


@router.get("/appointment-types/{type_id}/history", response_class=HTMLResponse, dependencies=[Depends(require_role(*ADMIN_SCHEDULING_VIEW))])
def type_history(request: Request, type_id: int, db: Session = Depends(get_db)):
    t = db.query(AppointmentType).filter(AppointmentType.id == type_id).first()
    if not t: return HTMLResponse("Not found", status_code=404)
    events = (db.query(AppointmentTypeAuditEvent).filter(AppointmentTypeAuditEvent.appointment_type_id == type_id)
              .order_by(AppointmentTypeAuditEvent.occurred_at.desc()).all())
    versions = sorted(t.versions, key=lambda v: v.version_number, reverse=True)
    return templates.TemplateResponse(request, "admin/scheduling/type_history.html",
        {"type_": t, "versions": versions, "events": events})


@router.get("/tests", response_class=HTMLResponse, dependencies=[Depends(require_role(*ADMIN_SCHEDULING_VIEW))])
def list_tests(request: Request, db: Session = Depends(get_db)):
    tests = db.query(DiagnosticTest).order_by(DiagnosticTest.display_order).all()
    return templates.TemplateResponse(request, "admin/scheduling/tests.html", {"tests": tests})


@router.post("/tests/new", dependencies=[Depends(require_role(*ADMIN_SCHEDULING_EDIT))])
def create_test(request: Request, code: str = Form(...), display_name: str = Form(...), calendar_abbreviation: str = Form(...),
    counts_toward_color: bool = Form(False), default_duration_minutes: str = Form(""),
    display_order: int = Form(0), csrf_token: str = Form(""), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    code = code.strip().upper()
    if db.query(DiagnosticTest).filter(DiagnosticTest.code == code).first():
        return HTMLResponse(f"Test code '{code}' already exists.", status_code=400)
    db.add(DiagnosticTest(code=code, display_name=display_name, calendar_abbreviation=calendar_abbreviation,
        active=True, counts_toward_color=counts_toward_color,
        default_duration_minutes=int(default_duration_minutes) if default_duration_minutes else None,
        display_order=display_order))
    db.commit()
    return RedirectResponse("/admin/scheduling/tests", status_code=303)


@router.post("/tests/{test_id}/toggle", dependencies=[Depends(require_role(*ADMIN_SCHEDULING_EDIT))])
def toggle_test(request: Request, test_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    t = db.query(DiagnosticTest).filter(DiagnosticTest.id == test_id).first()
    if t:
        t.active = not t.active
        db.commit()
    return RedirectResponse("/admin/scheduling/tests", status_code=303)


@router.get("/resources", response_class=HTMLResponse, dependencies=[Depends(require_role(*ADMIN_SCHEDULING_VIEW))])
def list_resources(request: Request, db: Session = Depends(get_db)):
    resources = db.query(Resource).order_by(Resource.resource_class, Resource.display_name).all()
    return templates.TemplateResponse(request, "admin/scheduling/resources.html", {"resources": resources})


@router.post("/resources/new", dependencies=[Depends(require_role(*ADMIN_SCHEDULING_EDIT))])
def create_resource(request: Request, code: str = Form(...), display_name: str = Form(...), resource_class: str = Form(...),
    exclusive: bool = Form(True), csrf_token: str = Form(""), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    code = code.strip().upper()
    if db.query(Resource).filter(Resource.code == code).first():
        return HTMLResponse(f"Resource code '{code}' already exists.", status_code=400)
    db.add(Resource(code=code, display_name=display_name, resource_class=resource_class, exclusive=exclusive, active=True))
    db.commit()
    return RedirectResponse("/admin/scheduling/resources", status_code=303)


@router.get("/availability", response_class=HTMLResponse, dependencies=[Depends(require_role(*ADMIN_SCHEDULING_VIEW))])
def list_availability(request: Request, db: Session = Depends(get_db)):
    templates_ = db.query(AvailabilityTemplate).order_by(AvailabilityTemplate.resource_id, AvailabilityTemplate.day_of_week).all()
    return templates.TemplateResponse(request, "admin/scheduling/availability.html",
        {"templates_": templates_, "resources": db.query(Resource).all(),
         "day_names": ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]})


@router.post("/availability/new", dependencies=[Depends(require_role(*ADMIN_SCHEDULING_EDIT))])
def create_availability(request: Request, resource_id: int = Form(...), day_of_week: int = Form(...), start_time: str = Form(...),
    end_time: str = Form(...), csrf_token: str = Form(""), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    db.add(AvailabilityTemplate(resource_id=resource_id, day_of_week=day_of_week, start_time=start_time,
        end_time=end_time, active=True))
    db.commit()
    return RedirectResponse("/admin/scheduling/availability", status_code=303)


@router.get("/provider-availability", response_class=HTMLResponse, dependencies=[Depends(require_role(*ADMIN_SCHEDULING_VIEW))])
def list_provider_availability(request: Request, db: Session = Depends(get_db)):
    """Provider-scoped counterpart to /availability above (which is
    resource-scoped only). Powers the real open-slot availability search
    (ehr/services/scheduling.py's find_open_slots, /appointments/availability)."""
    templates_ = (db.query(ProviderAvailabilityTemplate)
                  .order_by(ProviderAvailabilityTemplate.provider_id, ProviderAvailabilityTemplate.day_of_week)
                  .all())
    return templates.TemplateResponse(request, "admin/scheduling/provider_availability.html",
        {"templates_": templates_, "providers": db.query(Provider).all(),
         "day_names": ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]})


@router.post("/provider-availability/new", dependencies=[Depends(require_role(*ADMIN_SCHEDULING_EDIT))])
def create_provider_availability(request: Request, provider_id: int = Form(...), day_of_week: int = Form(...),
    start_time: str = Form(...), end_time: str = Form(...), csrf_token: str = Form(""), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    db.add(ProviderAvailabilityTemplate(provider_id=provider_id, day_of_week=day_of_week, start_time=start_time,
        end_time=end_time, active=True))
    db.commit()
    return RedirectResponse("/admin/scheduling/provider-availability", status_code=303)


@router.get("/holidays", response_class=HTMLResponse, dependencies=[Depends(require_role(*ADMIN_SCHEDULING_VIEW))])
def list_holidays(request: Request, db: Session = Depends(get_db)):
    closures = db.query(PracticeClosure).order_by(PracticeClosure.closure_date.desc()).all()
    return templates.TemplateResponse(request, "admin/scheduling/holidays.html", {"closures": closures})


@router.post("/holidays/new", dependencies=[Depends(require_role(*ADMIN_SCHEDULING_EDIT))])
def create_holiday(request: Request, closure_date: str = Form(...), label: str = Form(...), notes: str = Form(""),
    csrf_token: str = Form(""), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    if db.query(PracticeClosure).filter(PracticeClosure.closure_date == closure_date).first():
        return HTMLResponse(f"A closure already exists for {closure_date}.", status_code=400)
    db.add(PracticeClosure(closure_date=closure_date, label=label, notes=notes or None))
    db.commit()
    return RedirectResponse("/admin/scheduling/holidays", status_code=303)


@router.post("/holidays/{closure_id}/delete", dependencies=[Depends(require_role(*ADMIN_SCHEDULING_EDIT))])
def delete_holiday(request: Request, closure_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    c = db.query(PracticeClosure).filter(PracticeClosure.id == closure_id).first()
    if c:
        db.delete(c)
        db.commit()
    return RedirectResponse("/admin/scheduling/holidays", status_code=303)


@router.get("/audit", response_class=HTMLResponse, dependencies=[Depends(require_role(*ADMIN_SCHEDULING_VIEW))])
def scheduling_audit(request: Request, db: Session = Depends(get_db)):
    appt_events = db.query(AppointmentAuditEvent).order_by(AppointmentAuditEvent.occurred_at.desc()).limit(100).all()
    type_events = db.query(AppointmentTypeAuditEvent).order_by(AppointmentTypeAuditEvent.occurred_at.desc()).limit(100).all()
    return templates.TemplateResponse(request, "admin/scheduling/audit.html",
        {"appt_events": appt_events, "type_events": type_events})

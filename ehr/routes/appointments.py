import calendar as cal
from datetime import datetime, date, timedelta
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from ehr.models.database import (get_db, Appointment, Patient, Provider, AppointmentStatus, AppointmentType,
    AppointmentTypeVersion, DiagnosticTest, AppointmentTest, AppointmentAuditEvent, AppointmentResourceReservation)
from ehr.services import scheduling as sched
from ehr.env_info import EHR_ENV
from ehr.utils import patient_context
from ehr.auth.permissions import require_role, APPOINTMENT_EDIT, ROLE_LABELS

router = APIRouter(prefix="/appointments", tags=["appointments"])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS

# NOTE: This application has no authentication/authorization layer yet. Every
# permission check called for by the specification (appointments.create,
# appointments.override_conflict, appointment_types.manage, etc.) is a TODO
# here -- routes are open to anyone who can reach them. Add real checks once
# a User/role model exists.


def _bookable_type_versions(db: Session):
    """Latest active version per non-legacy, staff-bookable appointment type,
    ordered for the scheduler/legend (spec 8.1, 14.4)."""
    versions = (db.query(AppointmentTypeVersion)
                .join(AppointmentType, AppointmentType.id == AppointmentTypeVersion.appointment_type_id)
                .filter(AppointmentTypeVersion.active == True, AppointmentTypeVersion.staff_bookable == True,
                        AppointmentType.is_system_seeded == False)
                .order_by(AppointmentTypeVersion.display_order, AppointmentTypeVersion.appointment_type_id,
                          AppointmentTypeVersion.version_number.desc())
                .all())
    seen_type = set()
    result = []
    for v in versions:
        if v.appointment_type_id in seen_type:
            continue
        seen_type.add(v.appointment_type_id)
        result.append(v)
    result.sort(key=lambda v: v.display_order)
    return result


def _all_active_type_versions_for_legend(db: Session):
    """Includes non-bookable-but-active types for the calendar legend (spec 14.4);
    excludes the legacy system type unless it has appointments (handled in legend route)."""
    return (db.query(AppointmentTypeVersion)
            .join(AppointmentType, AppointmentType.id == AppointmentTypeVersion.appointment_type_id)
            .filter(AppointmentTypeVersion.active == True, AppointmentType.is_system_seeded == False)
            .order_by(AppointmentTypeVersion.display_order).all())


def _diagnostic_tests(db: Session):
    return db.query(DiagnosticTest).filter(DiagnosticTest.active == True).order_by(DiagnosticTest.display_order).all()


def _audit(db: Session, appointment_id: int, event_type: str, field_name=None, old_value=None,
           new_value=None, reason=None):
    db.add(AppointmentAuditEvent(appointment_id=appointment_id, event_type=event_type, field_name=field_name,
        old_value=str(old_value) if old_value is not None else None,
        new_value=str(new_value) if new_value is not None else None, reason=reason))


def _apply_scheduling_rules(db: Session, appt: Appointment, version: AppointmentTypeVersion, relationship: str,
                             is_follow_up: bool, scheduled_at: datetime, provider_id: int,
                             duration_override: int = None, override_reason: str = None,
                             conflict_override: bool = False, conflict_reason: str = None,
                             exclude_appointment_id: int = None):
    """Shared create/edit logic implementing spec sections 11-12. Raises ValueError
    with a user-facing message on any validation failure (caller turns this into a 4xx)."""
    closure = sched.find_closure(db, scheduled_at)
    if closure:
        raise ValueError(f"The practice is closed on this date: {closure.label}")

    if not sched.eligible_for_relationship(version, relationship):
        raise ValueError(
            f"{version.display_name} is not available for {relationship} patients. "
            f"Choose an eligible appointment type or change the patient relationship.")

    if duration_override is not None:
        if not sched.round_to_slot(duration_override) or not (5 <= duration_override <= 480):
            raise ValueError("Duration override must be a positive multiple of 5 minutes, between 5 and 480.")
        if not override_reason:
            raise ValueError("A reason is required when overriding the calculated duration.")
        duration = duration_override
        appt.duration_overridden = True
        appt.duration_override_reason = override_reason
    else:
        duration = sched.compute_duration_minutes(version, relationship)
        appt.duration_overridden = False
        appt.duration_override_reason = None

    buffer_before = version.buffer_before_minutes or 0
    buffer_after = version.buffer_after_minutes or 0
    occ_start, appt_end, occ_end = sched.compute_occupied_interval(scheduled_at, duration, buffer_before, buffer_after)

    conflict_message = None
    conflict = sched.find_provider_conflict(db, provider_id, occ_start, occ_end, exclude_appointment_id)
    if conflict:
        conflict_start = conflict.scheduled_at - timedelta(minutes=conflict.buffer_before_minutes or 0)
        conflict_end = conflict.scheduled_end_at or (conflict.scheduled_at + timedelta(minutes=conflict.duration_minutes or 0))
        conflict_message = (
            f"Scheduling conflict: provider is already booked for appointment #{conflict.id} "
            f"({conflict.patient.last_name}, {conflict_start.strftime('%I:%M %p')}-{conflict_end.strftime('%I:%M %p')}). "
            f"Requested interval {occ_start.strftime('%I:%M %p')}-{occ_end.strftime('%I:%M %p')} overlaps.")

    # Resource conflict check (spec 26.10 item 1): every appointment-type resource
    # requirement is resolved to a concrete Resource and checked for overlap against
    # other active reservations and resource-specific blocked availability exceptions,
    # using the exact same half-open interval math and ACTIVE_STATUSES rule as the
    # provider check above. `resource_plan` is always computed (even if a provider
    # conflict already exists) so it is available for reservation creation once any
    # conflict is either absent or overridden.
    resource_plan = sched.plan_resource_requirements(db, version, scheduled_at, duration, buffer_before, buffer_after)
    for requirement, resource, r_start, r_end in resource_plan:
        resource_conflict = sched.find_resource_conflict(db, resource.id, r_start, r_end, exclude_appointment_id)
        if resource_conflict:
            other_appt = resource_conflict.appointment
            if conflict_message is None:
                conflict_message = (
                    f"Scheduling conflict: {resource.display_name} is already reserved from "
                    f"{resource_conflict.reserved_start_at.strftime('%I:%M %p')} to "
                    f"{resource_conflict.reserved_end_at.strftime('%I:%M %p')} by another appointment "
                    f"(#{other_appt.id}, {other_appt.patient.last_name}). Requested interval "
                    f"{r_start.strftime('%I:%M %p')}-{r_end.strftime('%I:%M %p')} overlaps.")
            continue
        blocked = sched.find_resource_blocked_exception(db, resource.id, r_start, r_end)
        if blocked and conflict_message is None:
            conflict_message = (
                f"Scheduling conflict: {resource.display_name} is blocked from "
                f"{blocked.start_at.strftime('%I:%M %p')} to {blocked.end_at.strftime('%I:%M %p')}"
                + (f" ({blocked.reason})" if blocked.reason else "") + ".")

    if conflict_message and not conflict_override:
        raise ValueError(
            conflict_message +
            " To proceed anyway, check 'Override conflict' and provide a reason (appointments.override_conflict).")
    if conflict_message and conflict_override:
        if not conflict_reason:
            raise ValueError("A reason is required to override a scheduling conflict.")
        appt.conflict_overridden = True
        appt.conflict_override_reason = conflict_reason
    else:
        appt.conflict_overridden = False
        appt.conflict_override_reason = None

    appt.appointment_type_version_id = version.id
    appt.patient_relationship_at_booking = relationship
    appt.is_follow_up = is_follow_up
    appt.provider_id = provider_id
    appt.scheduled_at = scheduled_at
    appt.duration_minutes = duration
    appt.scheduled_end_at = appt_end
    appt.buffer_before_minutes = buffer_before
    appt.buffer_after_minutes = buffer_after
    appt.arrival_lead_minutes = version.arrival_lead_minutes or 0
    return appt


def _recolor(appt: Appointment, version: AppointmentTypeVersion):
    count = sched.count_countable_tests(appt.tests)
    color, reason = sched.resolve_color(version, appt.patient_relationship_at_booking, appt.is_follow_up, count)
    appt.resolved_color = color
    appt.resolved_color_reason = reason
    return count, color, reason


def _sync_resource_reservations(db: Session, appt: Appointment, version: AppointmentTypeVersion):
    """Deactivate any previous active resource reservations for this appointment
    (never hard-deleted -- history is preserved) and create fresh ones matching its
    current time/type, mirroring how the appointment's own booking snapshot fields
    are updated on edit/reschedule. Must be called AFTER _apply_scheduling_rules has
    already validated there is no unresolved conflict, and after `appt.id` exists."""
    for old in (db.query(AppointmentResourceReservation)
                .filter(AppointmentResourceReservation.appointment_id == appt.id,
                        AppointmentResourceReservation.active == True).all()):
        old.active = False
    plan = sched.plan_resource_requirements(db, version, appt.scheduled_at, appt.duration_minutes,
        appt.buffer_before_minutes, appt.buffer_after_minutes)
    for requirement, resource, start, end in plan:
        db.add(AppointmentResourceReservation(appointment_id=appt.id, resource_id=resource.id,
            reserved_start_at=start, reserved_end_at=end, active=True,
            override_reason=appt.conflict_override_reason if appt.conflict_overridden else None))


def _apply_tests(db: Session, appt: Appointment, test_ids):
    existing_by_test = {t.diagnostic_test_id: t for t in appt.tests}
    wanted = set(test_ids or [])
    for test_id, row in list(existing_by_test.items()):
        if test_id not in wanted and row.status == "active":
            row.status = "cancelled"
    for test_id in wanted:
        if test_id in existing_by_test:
            existing_by_test[test_id].status = "active"
            continue
        dt = db.query(DiagnosticTest).filter(DiagnosticTest.id == test_id).first()
        if not dt:
            continue
        db.add(AppointmentTest(appointment=appt, diagnostic_test_id=test_id,
                                counts_toward_color_snapshot=dt.counts_toward_color))


def _form_context(db: Session, patient_id=None, prefill_date=None, appt: Appointment = None, error=None,
                   posted=None):
    suggested = None
    if patient_id and prefill_date:
        suggested = sched.suggest_relationship(db, patient_id, prefill_date)
    elif patient_id and appt:
        suggested = sched.suggest_relationship(db, patient_id, appt.scheduled_at.date().isoformat())
    ctx_patient = None
    if appt is not None:
        ctx_patient = patient_context(appt.patient)
    elif patient_id:
        ctx_patient = patient_context(db.query(Patient).filter(Patient.id == patient_id).first())
    return {
        "patients": db.query(Patient).order_by(Patient.last_name).all(),
        "providers": db.query(Provider).all(),
        "types": _bookable_type_versions(db),
        "tests": _diagnostic_tests(db),
        "selected_patient_id": patient_id,
        "prefill_date": prefill_date,
        "suggested_relationship": suggested,
        "appt": appt,
        "error": error,
        "posted": posted or {},
        "context_patient": ctx_patient,
    }


@router.get("/", response_class=HTMLResponse)
def list_appointments(request: Request, patient_id: int = None, db: Session = Depends(get_db)):
    appts = db.query(Appointment).order_by(Appointment.scheduled_at).all()
    ctx_patient = patient_context(db.query(Patient).filter(Patient.id == patient_id).first()) if patient_id else None
    return templates.TemplateResponse(request, "appointments/list.html",
        {"appointments": appts, "context_patient": ctx_patient})


@router.get("/calendar", response_class=HTMLResponse)
def calendar_view(request: Request, year: int = None, month: int = None, patient_id: int = None, db: Session = Depends(get_db)):
    today = date.today()
    y = year or today.year
    m = month or today.month
    if m < 1: y -= 1; m = 12
    if m > 12: y += 1; m = 1

    cal.setfirstweekday(cal.SUNDAY)
    weeks = cal.monthcalendar(y, m)

    start = datetime(y, m, 1)
    end = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)
    appts = (db.query(Appointment)
             .filter(Appointment.scheduled_at >= start, Appointment.scheduled_at < end)
             .order_by(Appointment.scheduled_at).all())
    by_day = {}
    for a in appts:
        by_day.setdefault(a.scheduled_at.day, []).append(a)

    days_in_month = cal.monthrange(y, m)[1]
    day_dates = {d: date(y, m, d).isoformat() for d in range(1, days_in_month + 1)}

    prev_month, prev_year = (12, y - 1) if m == 1 else (m - 1, y)
    next_month, next_year = (1, y + 1) if m == 12 else (m + 1, y)

    return templates.TemplateResponse(request, "appointments/calendar.html", {
        "weeks": weeks, "by_day": by_day, "day_dates": day_dates, "year": y, "month": m,
        "month_name": cal.month_name[m], "today": today,
        "prev_year": prev_year, "prev_month": prev_month,
        "next_year": next_year, "next_month": next_month,
        "legend_types": _all_active_type_versions_for_legend(db),
        "context_patient": patient_context(db.query(Patient).filter(Patient.id == patient_id).first()) if patient_id else None,
    })


@router.get("/day", response_class=HTMLResponse)
def day_view(request: Request, date_str: str = None, provider_id: int = None,
             appointment_type_version_id: int = None, relationship: str = None, status: str = None,
             patient_id: int = None, db: Session = Depends(get_db)):
    d = date.fromisoformat(date_str) if date_str else date.today()
    start = datetime(d.year, d.month, d.day)
    end = start + timedelta(days=1)
    q = db.query(Appointment).filter(Appointment.scheduled_at >= start, Appointment.scheduled_at < end)
    if provider_id: q = q.filter(Appointment.provider_id == provider_id)
    if appointment_type_version_id: q = q.filter(Appointment.appointment_type_version_id == appointment_type_version_id)
    if relationship: q = q.filter(Appointment.patient_relationship_at_booking == relationship)
    if status: q = q.filter(Appointment.status == AppointmentStatus(status))
    appts = q.order_by(Appointment.scheduled_at).all()
    return templates.TemplateResponse(request, "appointments/day.html", {
        "day": d, "prev_day": (d - timedelta(days=1)).isoformat(), "next_day": (d + timedelta(days=1)).isoformat(),
        "appointments": appts, "providers": db.query(Provider).all(), "types": _bookable_type_versions(db),
        "statuses": list(AppointmentStatus), "provider_id": provider_id,
        "appointment_type_version_id": appointment_type_version_id, "relationship": relationship, "status": status,
        "legend_types": _all_active_type_versions_for_legend(db),
        "context_patient": patient_context(db.query(Patient).filter(Patient.id == patient_id).first()) if patient_id else None,
    })


@router.get("/week", response_class=HTMLResponse)
def week_view(request: Request, date_str: str = None, provider_id: int = None,
              appointment_type_version_id: int = None, relationship: str = None, status: str = None,
              patient_id: int = None, db: Session = Depends(get_db)):
    d = date.fromisoformat(date_str) if date_str else date.today()
    week_start = d - timedelta(days=(d.weekday() + 1) % 7)  # Sunday start, matching month view
    start = datetime(week_start.year, week_start.month, week_start.day)
    end = start + timedelta(days=7)
    q = db.query(Appointment).filter(Appointment.scheduled_at >= start, Appointment.scheduled_at < end)
    if provider_id: q = q.filter(Appointment.provider_id == provider_id)
    if appointment_type_version_id: q = q.filter(Appointment.appointment_type_version_id == appointment_type_version_id)
    if relationship: q = q.filter(Appointment.patient_relationship_at_booking == relationship)
    if status: q = q.filter(Appointment.status == AppointmentStatus(status))
    appts = q.order_by(Appointment.scheduled_at).all()
    days = [week_start + timedelta(days=i) for i in range(7)]
    by_day = {dd.isoformat(): [] for dd in days}
    for a in appts:
        by_day[a.scheduled_at.date().isoformat()].append(a)
    return templates.TemplateResponse(request, "appointments/week.html", {
        "week_start": week_start, "days": days, "by_day": by_day,
        "prev_week": (week_start - timedelta(days=7)).isoformat(), "next_week": (week_start + timedelta(days=7)).isoformat(),
        "providers": db.query(Provider).all(), "types": _bookable_type_versions(db), "statuses": list(AppointmentStatus),
        "provider_id": provider_id, "appointment_type_version_id": appointment_type_version_id,
        "relationship": relationship, "status": status, "today_iso": date.today().isoformat(),
        "legend_types": _all_active_type_versions_for_legend(db),
        "context_patient": patient_context(db.query(Patient).filter(Patient.id == patient_id).first()) if patient_id else None,
    })


@router.get("/availability", response_class=HTMLResponse)
def availability(request: Request, provider_id: int = Form(None), db: Session = Depends(get_db)):
    # Simplified per spec 19.2: full slot search UI is deferred (see report); this
    # endpoint exists so the route contract is satisfied and returns a usable page.
    return templates.TemplateResponse(request, "appointments/availability.html", {
        "providers": db.query(Provider).all(), "provider_id": provider_id})


@router.get("/new", response_class=HTMLResponse)
def new_appointment_form(request: Request, patient_id: int = None, date: str = None, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "appointments/form.html",
        _form_context(db, patient_id=patient_id, prefill_date=date))


@router.post("/new", response_class=HTMLResponse, dependencies=[Depends(require_role(*APPOINTMENT_EDIT))])
def create_appointment(request: Request, patient_id: int = Form(...), provider_id: int = Form(...),
    appointment_type_version_id: int = Form(...), scheduled_at: str = Form(...),
    relationship: str = Form(...), relationship_source: str = Form("automatic"),
    relationship_override_reason: str = Form(""), is_follow_up: bool = Form(False),
    duration_override: str = Form(""), duration_override_reason: str = Form(""),
    conflict_override: bool = Form(False), conflict_override_reason: str = Form(""),
    reason: str = Form(""), notes: str = Form(""), test_ids: list = Form([]),
    db: Session = Depends(get_db)):

    version = db.query(AppointmentTypeVersion).filter(AppointmentTypeVersion.id == appointment_type_version_id).first()
    when = datetime.fromisoformat(scheduled_at)
    posted = dict(patient_id=patient_id, provider_id=provider_id, appointment_type_version_id=appointment_type_version_id,
                  scheduled_at=scheduled_at, relationship=relationship, is_follow_up=is_follow_up,
                  reason=reason, notes=notes, test_ids=[int(t) for t in test_ids] if test_ids else [])

    def fail(msg):
        ctx = _form_context(db, patient_id=patient_id, prefill_date=when.date().isoformat(), error=msg, posted=posted)
        return templates.TemplateResponse(request, "appointments/form.html", ctx, status_code=400)

    if not version or not version.active or version.appointment_type.is_system_seeded:
        return fail("Selected appointment type is not available for booking.")
    if relationship not in ("new", "established"):
        return fail("Patient relationship must be 'new' or 'established'.")
    if relationship_source == "manual_override" and not relationship_override_reason:
        return fail("A reason is required when overriding the suggested new/established classification.")

    appt = Appointment(patient_id=patient_id, reason=reason, notes=notes,
        patient_relationship_source=relationship_source,
        patient_relationship_override_reason=relationship_override_reason or None,
        status=AppointmentStatus.scheduled)

    duration_override_val = int(duration_override) if duration_override else None
    try:
        _apply_scheduling_rules(db, appt, version, relationship, is_follow_up, when, provider_id,
            duration_override=duration_override_val, override_reason=duration_override_reason or None,
            conflict_override=conflict_override, conflict_reason=conflict_override_reason or None)
    except ValueError as e:
        return fail(str(e))

    db.add(appt); db.flush()
    _apply_tests(db, appt, posted["test_ids"])
    _sync_resource_reservations(db, appt, version)
    db.flush()
    count, color, reason_code = _recolor(appt, version)
    _audit(db, appt.id, "created", reason="Appointment created")
    if appt.duration_overridden:
        _audit(db, appt.id, "duration_override", new_value=appt.duration_minutes, reason=appt.duration_override_reason)
    if appt.conflict_overridden:
        _audit(db, appt.id, "conflict_override", reason=appt.conflict_override_reason)
    db.commit(); db.refresh(appt)
    return RedirectResponse(f"/appointments/{appt.id}", status_code=303)


@router.get("/{appt_id}", response_class=HTMLResponse)
def appointment_detail(request: Request, appt_id: int, db: Session = Depends(get_db)):
    a = db.query(Appointment).filter(Appointment.id == appt_id).first()
    if not a: return HTMLResponse("Not found", status_code=404)
    audit = (db.query(AppointmentAuditEvent).filter(AppointmentAuditEvent.appointment_id == appt_id)
             .order_by(AppointmentAuditEvent.occurred_at.desc()).all())
    return templates.TemplateResponse(request, "appointments/detail.html",
        {"appt": a, "statuses": list(AppointmentStatus), "audit_events": audit,
         "context_patient": patient_context(a.patient)})


@router.get("/{appt_id}/edit", response_class=HTMLResponse)
def edit_appointment_form(request: Request, appt_id: int, db: Session = Depends(get_db)):
    a = db.query(Appointment).filter(Appointment.id == appt_id).first()
    if not a: return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "appointments/edit.html",
        _form_context(db, patient_id=a.patient_id, appt=a))


@router.post("/{appt_id}/edit", response_class=HTMLResponse, dependencies=[Depends(require_role(*APPOINTMENT_EDIT))])
def update_appointment(request: Request, appt_id: int, provider_id: int = Form(...),
    appointment_type_version_id: int = Form(...), scheduled_at: str = Form(...),
    relationship: str = Form(...), relationship_source: str = Form("automatic"),
    relationship_override_reason: str = Form(""), is_follow_up: bool = Form(False),
    duration_override: str = Form(""), duration_override_reason: str = Form(""),
    conflict_override: bool = Form(False), conflict_override_reason: str = Form(""),
    reason: str = Form(""), notes: str = Form(""), status: str = Form(None), test_ids: list = Form([]),
    db: Session = Depends(get_db)):
    a = db.query(Appointment).filter(Appointment.id == appt_id).first()
    if not a: return HTMLResponse("Not found", status_code=404)

    version = db.query(AppointmentTypeVersion).filter(AppointmentTypeVersion.id == appointment_type_version_id).first()
    when = datetime.fromisoformat(scheduled_at)

    def fail(msg):
        ctx = _form_context(db, patient_id=a.patient_id, appt=a, error=msg)
        return templates.TemplateResponse(request, "appointments/edit.html", ctx, status_code=400)

    if not version or not version.active:
        return fail("Selected appointment type is not available for booking.")
    if relationship not in ("new", "established"):
        return fail("Patient relationship must be 'new' or 'established'.")

    old_when, old_provider, old_type = a.scheduled_at, a.provider_id, a.appointment_type_version_id
    a.reason = reason; a.notes = notes
    duration_override_val = int(duration_override) if duration_override else None
    try:
        _apply_scheduling_rules(db, a, version, relationship, is_follow_up, when, provider_id,
            duration_override=duration_override_val, override_reason=duration_override_reason or None,
            conflict_override=conflict_override, conflict_reason=conflict_override_reason or None,
            exclude_appointment_id=a.id)
    except ValueError as e:
        return fail(str(e))

    _apply_tests(db, a, [int(t) for t in test_ids] if test_ids else [])
    _sync_resource_reservations(db, a, version)
    db.flush()
    _recolor(a, version)
    if status:
        a.status = AppointmentStatus(status)
    if old_when != when:
        _audit(db, a.id, "rescheduled", field_name="scheduled_at", old_value=old_when, new_value=when)
    if old_provider != provider_id:
        _audit(db, a.id, "provider_changed", field_name="provider_id", old_value=old_provider, new_value=provider_id)
    if old_type != version.id:
        _audit(db, a.id, "type_changed", field_name="appointment_type_version_id", old_value=old_type, new_value=version.id)
    a.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(f"/appointments/{a.id}", status_code=303)


@router.post("/{appt_id}/reschedule", dependencies=[Depends(require_role(*APPOINTMENT_EDIT))])
def reschedule_appointment(appt_id: int, scheduled_at: str = Form(...),
    conflict_override: bool = Form(False), conflict_override_reason: str = Form(""),
    db: Session = Depends(get_db)):
    a = db.query(Appointment).filter(Appointment.id == appt_id).first()
    if not a: return HTMLResponse("Not found", status_code=404)
    version = a.appointment_type_version
    when = datetime.fromisoformat(scheduled_at)
    old_when = a.scheduled_at
    try:
        _apply_scheduling_rules(db, a, version, a.patient_relationship_at_booking, a.is_follow_up, when,
            a.provider_id, duration_override=a.duration_minutes if a.duration_overridden else None,
            override_reason=a.duration_override_reason, conflict_override=conflict_override,
            conflict_reason=conflict_override_reason or None, exclude_appointment_id=a.id)
    except ValueError as e:
        return HTMLResponse(str(e), status_code=400)
    _sync_resource_reservations(db, a, version)
    _audit(db, a.id, "rescheduled", field_name="scheduled_at", old_value=old_when, new_value=when)
    a.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(f"/appointments/{appt_id}", status_code=303)


@router.post("/{appt_id}/status", dependencies=[Depends(require_role(*APPOINTMENT_EDIT))])
def update_status(appt_id: int, status: str = Form(...), db: Session = Depends(get_db)):
    a = db.query(Appointment).filter(Appointment.id == appt_id).first()
    if not a: return HTMLResponse("Not found", status_code=404)
    try:
        new_status = AppointmentStatus(status)
    except ValueError:
        return HTMLResponse("Invalid status value.", status_code=400)
    old_status = a.status
    a.status = new_status
    a.updated_at = datetime.utcnow()
    _audit(db, a.id, "status_changed", field_name="status",
           old_value=old_status.value if old_status else None, new_value=new_status.value)
    db.commit()
    return RedirectResponse(f"/appointments/{appt_id}", status_code=303)

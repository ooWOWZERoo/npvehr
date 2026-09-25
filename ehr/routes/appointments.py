import calendar as cal
from datetime import datetime, date, timedelta
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session
from ehr.models.database import (get_db, Appointment, Patient, Provider, AppointmentStatus, AppointmentType,
    AppointmentTypeVersion, DiagnosticTest, AppointmentTest, AppointmentAuditEvent, AppointmentResourceReservation,
    Resource, WaitlistEntry, User, AppointmentReminder, WaitlistNotification)
from ehr.services import scheduling as sched
from ehr.services import notifications as notify
from ehr.services import cpt_mapper
from ehr.env_info import EHR_ENV, CRON_SECRET
from ehr.utils import patient_context
from ehr.auth.permissions import require_role, APPOINTMENT_EDIT, ROLE_LABELS
from ehr.auth.deps import get_current_user
from ehr.auth import csrf

router = APIRouter(prefix="/appointments", tags=["appointments"])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV

# Separate, unauthenticated router for the reminder cron endpoint. Every route
# on `router` above gets the session-auth dependency applied at inclusion time
# in ehr/app.py -- fine for staff-facing pages, but Vercel's Cron Job caller
# has no session cookie, only a bearer token (see CRON_SECRET in
# ehr/env_info.py). This router is included directly on `app` with no
# dependency, so it must do its own auth check in the route body.
cron_router = APIRouter(prefix="/appointments", tags=["appointments-cron"])

REMINDER_LOOKAHEAD_HOURS = 24


@cron_router.get("/reminders/run")
def run_reminder_scan(request: Request, db: Session = Depends(get_db)):
    """Cron-triggered reminder scan (Phase 3 -- BUILD_BACKLOG.md 5a): finds
    every scheduled appointment starting within REMINDER_LOOKAHEAD_HOURS and
    sends (mock) reminder notices, per patient opt-in, for any channel that
    hasn't already been sent for this appointment (send_appointment_notice's
    own idempotency check). GET, not POST -- Vercel Cron Jobs always trigger
    with a GET request (see vercel.json's `crons` entry). Authenticated the
    same way Vercel signs those requests: `Authorization: Bearer
    $CRON_SECRET`. Not CSRF-protected -- CSRF guards session-authenticated
    browser forms, and this endpoint is neither (no session, no form), it's
    a bearer-token service-to-service call, same posture as an API key."""
    import hmac
    auth = request.headers.get("authorization") or ""
    expected = f"Bearer {CRON_SECRET}"
    if not hmac.compare_digest(auth, expected):
        return HTMLResponse("Forbidden", status_code=403)

    now = datetime.utcnow()
    window_end = now + timedelta(hours=REMINDER_LOOKAHEAD_HOURS)
    upcoming = (db.query(Appointment)
                .filter(Appointment.status == AppointmentStatus.scheduled,
                        Appointment.scheduled_at >= now,
                        Appointment.scheduled_at <= window_end)
                .all())
    sent_count = 0
    for appt in upcoming:
        results = notify.send_appointment_notice(db, appt, "reminder")
        sent_count += sum(1 for r in results if r.status == "sent")
    return JSONResponse({"ok": True, "appointments_checked": len(upcoming), "notices_sent": sent_count})
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS

# Every route in this router requires a valid session (applied at router-inclusion
# time in ehr/app.py); mutation routes additionally require APPOINTMENT_EDIT via
# require_role() below, per the v2.4 authentication/role build.


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


def _qi(v):
    """Converts an optional int-shaped query param to int, treating an empty
    string the same as absent. A plain `int = None` FastAPI param parameter
    rejects '' with a 422 -- but an HTML <select> with an empty-value "All"
    option (e.g. the board's provider/type/room filters) submits exactly that
    when chosen, so every query param of this shape must go through this
    first rather than declaring the FastAPI param type as int directly."""
    return int(v) if v not in (None, "") else None


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
        "providers": sched.bookable_providers(db, include_id=appt.provider_id if appt else None),
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


def _board_context(request: Request, db: Session, initial_view: str, initial_date: date, providers_mode: bool,
                    provider_id=None, appointment_type_version_id=None, relationship=None, status=None,
                    room_resource_id=None, has_notes=None, patient_id=None):
    """Shared context for the FullCalendar-driven board (Calendar & Appointments
    UX Overhaul Phase 1, BUILD_BACKLOG.md 5a) -- all of /calendar, /day, /week,
    and /board render the same template, differing only in initial_view/date
    and providers_mode. Appointment data itself is never queried here; the
    template's own JS fetches it live from /appointments/feed.json, so this
    context only supplies filter-dropdown options and the initial state to
    load the page in."""
    providers = db.query(Provider).order_by(Provider.last_name).all()
    # Slot/duration reconciliation follow-up: each provider's effective
    # offering granularity, so the board's calendar grid (visual slot lines
    # and drag/resize snapping) matches what find_open_slots actually offers
    # for booking -- not just a hardcoded 5-minute grid. Keyed by provider id
    # (string, since it becomes a JS object key) for the template's JS.
    granularity_by_provider = {str(p.id): sched.get_effective_slot_granularity(db, p.id) for p in providers}
    from ehr.models.database import SchedulingSettings
    settings_row = db.query(SchedulingSettings).filter(SchedulingSettings.id == 1).first()
    default_granularity = settings_row.default_slot_granularity_minutes if settings_row else 5
    return {
        "initial_view": initial_view, "initial_date": initial_date.isoformat(), "providers_mode": providers_mode,
        "providers": providers,
        "rooms": db.query(Resource).filter(Resource.resource_class == "room", Resource.active == True)
                   .order_by(Resource.display_name).all(),
        "types": _bookable_type_versions(db), "statuses": list(AppointmentStatus),
        "provider_id": provider_id, "appointment_type_version_id": appointment_type_version_id,
        "relationship": relationship, "status": status, "room_resource_id": room_resource_id, "has_notes": has_notes,
        "legend_types": _all_active_type_versions_for_legend(db),
        "context_patient": patient_context(db.query(Patient).filter(Patient.id == patient_id).first()) if patient_id else None,
        "granularity_by_provider": granularity_by_provider, "default_granularity": default_granularity,
    }


@router.get("/calendar", response_class=HTMLResponse)
def calendar_view(request: Request, year: int = None, month: int = None, provider_id: str = None,
                   appointment_type_version_id: str = None, relationship: str = None, status: str = None,
                   room_resource_id: str = None, has_notes: str = None, patient_id: int = None,
                   db: Session = Depends(get_db)):
    today = date.today()
    y, m = year or today.year, month or today.month
    d = date(y, m, 1)
    return templates.TemplateResponse(request, "appointments/board.html", _board_context(
        request, db, "dayGridMonth", d, False, _qi(provider_id), _qi(appointment_type_version_id), relationship,
        status, _qi(room_resource_id), has_notes, patient_id))


@router.get("/day", response_class=HTMLResponse)
def day_view(request: Request, date_str: str = None, provider_id: str = None,
             appointment_type_version_id: str = None, relationship: str = None, status: str = None,
             room_resource_id: str = None, has_notes: str = None, patient_id: int = None,
             db: Session = Depends(get_db)):
    d = date.fromisoformat(date_str) if date_str else date.today()
    return templates.TemplateResponse(request, "appointments/board.html", _board_context(
        request, db, "timeGridDay", d, False, _qi(provider_id), _qi(appointment_type_version_id), relationship,
        status, _qi(room_resource_id), has_notes, patient_id))


@router.get("/week", response_class=HTMLResponse)
def week_view(request: Request, date_str: str = None, provider_id: str = None,
              appointment_type_version_id: str = None, relationship: str = None, status: str = None,
              room_resource_id: str = None, has_notes: str = None, patient_id: int = None,
              db: Session = Depends(get_db)):
    d = date.fromisoformat(date_str) if date_str else date.today()
    return templates.TemplateResponse(request, "appointments/board.html", _board_context(
        request, db, "timeGridWeek", d, False, _qi(provider_id), _qi(appointment_type_version_id), relationship,
        status, _qi(room_resource_id), has_notes, patient_id))


@router.get("/board", response_class=HTMLResponse)
def board_view(request: Request, date_str: str = None, appointment_type_version_id: str = None,
               relationship: str = None, status: str = None, room_resource_id: str = None,
               has_notes: str = None, patient_id: int = None, db: Session = Depends(get_db)):
    """Multi-provider board (spec: 'multi-provider or multi-location grids') --
    the free-tier workaround for FullCalendar Premium's paid resource-timeline
    plugin: one free timeGridDay calendar per active provider, laid out in a
    CSS grid and driven by one shared toolbar, rather than a single calendar
    with true unified resource columns. See BUILD_BACKLOG.md 5a for the
    licensing tradeoff this was chosen over."""
    d = date.fromisoformat(date_str) if date_str else date.today()
    return templates.TemplateResponse(request, "appointments/board.html", _board_context(
        request, db, "timeGridDay", d, True, None, _qi(appointment_type_version_id), relationship, status,
        _qi(room_resource_id), has_notes, patient_id))


@router.get("/feed.json")
def appointments_feed(request: Request, start: str = None, end: str = None, provider_id: str = None,
                       appointment_type_version_id: str = None, relationship: str = None, status: str = None,
                       room_resource_id: str = None, has_notes: str = None, db: Session = Depends(get_db)):
    """JSON events feed for the board's FullCalendar instance(s) (BUILD_BACKLOG.md
    5a Phase 1). FullCalendar calls this itself with the currently-visible date
    range every time the user navigates or switches views -- `start`/`end` are
    its own ISO datetime strings, not something a person types. Route must be
    registered before GET /{appt_id} (a literal path segment ahead of a
    parameterized one) or FastAPI would try to parse "feed.json" as an int id."""
    provider_id, appointment_type_version_id, room_resource_id = (
        _qi(provider_id), _qi(appointment_type_version_id), _qi(room_resource_id))
    q = db.query(Appointment)
    if start:
        q = q.filter(Appointment.scheduled_at >= datetime.fromisoformat(start[:19]))
    if end:
        q = q.filter(Appointment.scheduled_at < datetime.fromisoformat(end[:19]))
    if provider_id:
        q = q.filter(Appointment.provider_id == provider_id)
    if appointment_type_version_id:
        q = q.filter(Appointment.appointment_type_version_id == appointment_type_version_id)
    if relationship:
        q = q.filter(Appointment.patient_relationship_at_booking == relationship)
    if status:
        q = q.filter(Appointment.status == AppointmentStatus(status))
    if has_notes == "yes":
        q = q.filter(Appointment.notes.isnot(None), Appointment.notes != "")
    elif has_notes == "no":
        q = q.filter(or_(Appointment.notes.is_(None), Appointment.notes == ""))
    if room_resource_id:
        q = (q.join(AppointmentResourceReservation, AppointmentResourceReservation.appointment_id == Appointment.id)
             .filter(AppointmentResourceReservation.resource_id == room_resource_id,
                     AppointmentResourceReservation.active == True))
    appts = q.order_by(Appointment.scheduled_at).all()
    events = []
    for a in appts:
        rooms = [r.resource.display_name for r in a.resource_reservations
                 if r.active and r.resource and r.resource.resource_class == "room"]
        events.append({
            "id": a.id,
            "title": f"{a.patient.last_name}, {a.patient.first_name}",
            "start": a.scheduled_at.isoformat(),
            "end": (a.scheduled_end_at or a.scheduled_at).isoformat(),
            "color": a.resolved_color or "#94a3b8",
            "extendedProps": {
                "patientName": f"{a.patient.last_name}, {a.patient.first_name}",
                "patientPhone": a.patient.phone or "",
                "providerId": a.provider_id,
                "providerName": f"Dr. {a.provider.last_name}",
                "appointmentTypeVersionId": a.appointment_type_version_id,
                "typeName": a.appointment_type_version.display_name if a.appointment_type_version else "Unclassified",
                "typeAbbrev": a.appointment_type_version.calendar_abbreviation if a.appointment_type_version else "?",
                "status": a.status.value,
                "relationship": a.patient_relationship_at_booking,
                "rooms": rooms,
                "hasNotes": bool(a.notes and a.notes.strip()),
                "conflictOverridden": a.conflict_overridden,
                "durationMinutes": a.duration_minutes,
            },
        })
    return events


@router.get("/availability", response_class=HTMLResponse)
def availability(request: Request, provider_id: int = None, appointment_type_version_id: int = None,
                  relationship: str = None, date_str: str = None, db: Session = Depends(get_db)):
    """Real open-slot search (spec 19.2) -- see ehr/services/scheduling.py's
    find_open_slots. Previously a placeholder page with no logic at all; the
    old version also read provider_id via Form(None) on a GET route, which
    never actually receives a value (a GET <form> submits the query string,
    not a request body, which is what Form() reads) -- these are now plain
    query parameters, which FastAPI binds from the query string by default."""
    providers = sched.bookable_providers(db)
    types = _bookable_type_versions(db)
    target_date = date.fromisoformat(date_str) if date_str else date.today()
    slots, version, error = [], None, None
    if provider_id and appointment_type_version_id and relationship:
        version = db.query(AppointmentTypeVersion).filter(
            AppointmentTypeVersion.id == appointment_type_version_id).first()
        if not version or not version.active:
            error = "Selected appointment type is not available for booking."
        elif relationship not in ("new", "established"):
            error = "Patient relationship must be 'new' or 'established'."
        else:
            try:
                duration_minutes = sched.compute_duration_minutes(version, relationship)
                slots = sched.find_open_slots(db, provider_id, target_date, duration_minutes, version=version)
            except ValueError as e:
                error = str(e)
    return templates.TemplateResponse(request, "appointments/availability.html", {
        "providers": providers, "types": types, "provider_id": provider_id,
        "appointment_type_version_id": appointment_type_version_id, "relationship": relationship,
        "target_date": target_date, "slots": slots, "version": version, "error": error})


@router.get("/waitlist", response_class=HTMLResponse)
def staff_waitlist_queue(request: Request, db: Session = Depends(get_db)):
    """Staff-facing waitlist queue (Calendar & Appointments UX Overhaul Phase
    2 -- BUILD_BACKLOG.md 5a): every active entry across all patients, urgent
    first, oldest-first within a priority tier (same ordering
    find_matching_waitlist_entries uses for a specific freed slot). Adding an
    entry stays per-patient (patients/{id}/waitlist); this is the front-desk
    view for working the queue as a whole."""
    from sqlalchemy import case
    entries = (db.query(WaitlistEntry).filter(WaitlistEntry.status == "active")
               .order_by(case((WaitlistEntry.priority == "urgent", 0), else_=1), WaitlistEntry.created_at).all())
    # Waitlist auto-notify audit trail (BUILD_BACKLOG.md 5a follow-up): recent
    # attempted sends, newest first, so staff can see the queue is actually
    # being worked automatically, not just sitting there.
    notifications = (db.query(WaitlistNotification)
                      .order_by(WaitlistNotification.created_at.desc()).limit(50).all())
    return templates.TemplateResponse(request, "appointments/waitlist.html",
        {"entries": entries, "notifications": notifications})


@router.get("/reminders", response_class=HTMLResponse)
def reminders_audit_log(request: Request, db: Session = Depends(get_db)):
    """Automated Confirmations & Reminders (Phase 3 -- BUILD_BACKLOG.md 5a):
    staff-facing audit trail of every attempted send (sent, skipped for no
    opt-in, or failed), newest first. Mirrors the Waitlist queue page's
    pattern for a global, non-patient-scoped staff view."""
    entries = (db.query(AppointmentReminder)
               .order_by(AppointmentReminder.created_at.desc()).limit(200).all())
    return templates.TemplateResponse(request, "appointments/reminders.html", {"entries": entries})


@router.get("/new", response_class=HTMLResponse)
def new_appointment_form(request: Request, patient_id: int = None, date: str = None, provider_id: int = None,
                          db: Session = Depends(get_db)):
    # provider_id here is a prefill only (e.g. the Waitlist queue's "Book"
    # link for an entry with a specific provider preference) -- reuses the
    # same `posted.provider_id` template hook the form already checks after
    # a failed submission, so no template change is needed for this to work.
    posted = {"provider_id": provider_id} if provider_id else None
    return templates.TemplateResponse(request, "appointments/form.html",
        _form_context(db, patient_id=patient_id, prefill_date=date, posted=posted))


@router.post("/new", response_class=HTMLResponse, dependencies=[Depends(require_role(*APPOINTMENT_EDIT))])
def create_appointment(request: Request, patient_id: int = Form(...), provider_id: int = Form(...),
    appointment_type_version_id: int = Form(...), scheduled_at: str = Form(...),
    relationship: str = Form(...), relationship_source: str = Form("automatic"),
    relationship_override_reason: str = Form(""), is_follow_up: bool = Form(False),
    duration_override: str = Form(""), duration_override_reason: str = Form(""),
    conflict_override: bool = Form(False), conflict_override_reason: str = Form(""),
    reason: str = Form(""), notes: str = Form(""), test_ids: list = Form([]),
    csrf_token: str = Form(""), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)

    version = db.query(AppointmentTypeVersion).filter(AppointmentTypeVersion.id == appointment_type_version_id).first()
    posted = dict(patient_id=patient_id, provider_id=provider_id, appointment_type_version_id=appointment_type_version_id,
                  scheduled_at=scheduled_at, relationship=relationship, is_follow_up=is_follow_up,
                  reason=reason, notes=notes, test_ids=[int(t) for t in test_ids] if test_ids else [])

    try:
        when = datetime.fromisoformat(scheduled_at)
    except (ValueError, TypeError):
        ctx = _form_context(db, patient_id=patient_id, prefill_date=None,
                             error="Invalid appointment date/time.", posted=posted)
        return templates.TemplateResponse(request, "appointments/form.html", ctx, status_code=400)

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
        status=AppointmentStatus.scheduled,
        created_by_user_id=user.id, updated_by_user_id=user.id)

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
    notify.send_appointment_notice(db, appt, "confirmation")
    return RedirectResponse(f"/appointments/{appt.id}", status_code=303)


@router.get("/{appt_id}", response_class=HTMLResponse)
def appointment_detail(request: Request, appt_id: int, db: Session = Depends(get_db)):
    a = db.query(Appointment).filter(Appointment.id == appt_id).first()
    if not a: return HTMLResponse("Not found", status_code=404)
    audit = (db.query(AppointmentAuditEvent).filter(AppointmentAuditEvent.appointment_id == appt_id)
             .order_by(AppointmentAuditEvent.occurred_at.desc()).all())
    # Waitlist Management (BUILD_BACKLOG.md 5a Phase 2): a cancelled appointment
    # frees its slot, so surface any active waitlist entries that match it
    # (same provider/type, desired date range covers this date) for staff to
    # call. Each match is auto-notified once (BUILD_BACKLOG.md 5a's waitlist
    # auto-notify follow-up, wired into every place an appointment is
    # cancelled -- see ehr.services.notifications.send_waitlist_opening_notices);
    # notified_entry_ids lets this page show which matches already got a
    # notice sent, so staff know a manual call isn't the only thing that happened.
    waitlist_matches = []
    notified_entry_ids = set()
    if a.status == AppointmentStatus.cancelled:
        waitlist_matches = sched.find_matching_waitlist_entries(
            db, a.provider_id, a.appointment_type_version_id, a.scheduled_at.date())
        if waitlist_matches:
            notified_entry_ids = {row.waitlist_entry_id for row in
                db.query(WaitlistNotification.waitlist_entry_id)
                .filter(WaitlistNotification.appointment_id == a.id, WaitlistNotification.status == "sent")
                .all()}
    # Two-flow billing preview (Phase 2, BUILD_BACKLOG.md 0a): suggested only
    # when the appointment hasn't already had a flow set (either
    # automatically or by a staff override) -- once set, the stored value is
    # what the check-in card and the billing preview both use.
    suggested_visit_flow = a.visit_flow or cpt_mapper.suggest_visit_flow(db, a.patient_id)
    return templates.TemplateResponse(request, "appointments/detail.html",
        {"appt": a, "statuses": list(AppointmentStatus), "audit_events": audit,
         "waitlist_matches": waitlist_matches, "notified_entry_ids": notified_entry_ids,
         "suggested_visit_flow": suggested_visit_flow,
         "context_patient": patient_context(a.patient)})


@router.post("/{appt_id}/visit-flow", dependencies=[Depends(require_role(*APPOINTMENT_EDIT))])
def update_visit_flow(request: Request, appt_id: int, visit_flow: str = Form(...),
    csrf_token: str = Form(""), db: Session = Depends(get_db)):
    """Check-in step of the two-flow billing preview (Phase 2, BUILD_BACKLOG.md
    0a): staff confirm or override which flow (vision plan vs. medical
    insurance) this visit follows. Decision support only -- this never
    submits or transmits anything, it just records which billing-preview
    template the exam detail page should render."""
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    a = db.query(Appointment).filter(Appointment.id == appt_id).first()
    if not a: return HTMLResponse("Not found", status_code=404)
    if visit_flow not in ("vision", "medical"):
        return HTMLResponse("Invalid visit flow.", status_code=400)
    suggested = cpt_mapper.suggest_visit_flow(db, a.patient_id)
    a.visit_flow = visit_flow
    a.visit_flow_source = "automatic" if visit_flow == suggested else "manual_override"
    db.commit()
    return RedirectResponse(f"/appointments/{appt_id}", status_code=303)


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
    csrf_token: str = Form(""), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    a = db.query(Appointment).filter(Appointment.id == appt_id).first()
    if not a: return HTMLResponse("Not found", status_code=404)

    version = db.query(AppointmentTypeVersion).filter(AppointmentTypeVersion.id == appointment_type_version_id).first()

    def fail(msg):
        ctx = _form_context(db, patient_id=a.patient_id, appt=a, error=msg)
        return templates.TemplateResponse(request, "appointments/edit.html", ctx, status_code=400)

    try:
        when = datetime.fromisoformat(scheduled_at)
    except (ValueError, TypeError):
        return fail("Invalid appointment date/time.")

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
    old_status = a.status
    if status:
        a.status = AppointmentStatus(status)
    if old_when != when:
        _audit(db, a.id, "rescheduled", field_name="scheduled_at", old_value=old_when, new_value=when)
    if old_provider != provider_id:
        _audit(db, a.id, "provider_changed", field_name="provider_id", old_value=old_provider, new_value=provider_id)
    if old_type != version.id:
        _audit(db, a.id, "type_changed", field_name="appointment_type_version_id", old_value=old_type, new_value=version.id)
    a.updated_at = datetime.utcnow()
    a.updated_by_user_id = user.id
    db.commit()
    if old_status != AppointmentStatus.cancelled and a.status == AppointmentStatus.cancelled:
        notify.send_waitlist_opening_notices(db, a)
    return RedirectResponse(f"/appointments/{a.id}", status_code=303)


@router.post("/{appt_id}/reschedule", dependencies=[Depends(require_role(*APPOINTMENT_EDIT))])
def reschedule_appointment(request: Request, appt_id: int, scheduled_at: str = Form(...),
    duration_minutes: str = Form(""),
    conflict_override: bool = Form(False), conflict_override_reason: str = Form(""),
    csrf_token: str = Form(""), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Reschedules an existing appointment's time (and, if given, its duration
    -- an event resize). Reuses _apply_scheduling_rules exactly as the full
    edit form does, so a drag/resize on the board (BUILD_BACKLOG.md 5a Phase 1)
    is blocked by the same provider/resource conflict check, with the same
    override+reason escape hatch, as every other reschedule path in this app.
    Responds as JSON when the caller sends `Accept: application/json` (the
    board's own fetch() calls do) so it can revert a rejected drag/resize
    without a full page navigation; otherwise keeps the original
    redirect-on-success / plain-text-on-error behavior for any future
    traditional-form caller."""
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    wants_json = "application/json" in (request.headers.get("accept") or "")

    def fail(msg, status_code):
        if wants_json:
            return JSONResponse({"ok": False, "error": msg}, status_code=status_code)
        return HTMLResponse(msg, status_code=status_code if status_code != 409 else 400)

    a = db.query(Appointment).filter(Appointment.id == appt_id).first()
    if not a:
        return fail("Not found", 404)
    version = a.appointment_type_version
    try:
        when = datetime.fromisoformat(scheduled_at)
    except (ValueError, TypeError):
        return fail("Invalid appointment date/time.", 400)

    duration_override_val = a.duration_minutes if a.duration_overridden else None
    override_reason_val = a.duration_override_reason
    if duration_minutes:
        try:
            duration_override_val = int(duration_minutes)
            override_reason_val = "Resized via calendar drag"
        except ValueError:
            return fail("Invalid duration.", 400)

    old_when = a.scheduled_at
    try:
        _apply_scheduling_rules(db, a, version, a.patient_relationship_at_booking, a.is_follow_up, when,
            a.provider_id, duration_override=duration_override_val, override_reason=override_reason_val,
            conflict_override=conflict_override, conflict_reason=conflict_override_reason or None,
            exclude_appointment_id=a.id)
    except ValueError as e:
        return fail(str(e), 409)
    _sync_resource_reservations(db, a, version)
    _audit(db, a.id, "rescheduled", field_name="scheduled_at", old_value=old_when, new_value=when)
    a.updated_at = datetime.utcnow()
    a.updated_by_user_id = user.id
    db.commit()
    if wants_json:
        return JSONResponse({"ok": True, "id": a.id, "scheduled_at": a.scheduled_at.isoformat(),
            "scheduled_end_at": a.scheduled_end_at.isoformat() if a.scheduled_end_at else None})
    return RedirectResponse(f"/appointments/{appt_id}", status_code=303)


@router.post("/{appt_id}/status", dependencies=[Depends(require_role(*APPOINTMENT_EDIT))])
def update_status(request: Request, appt_id: int, status: str = Form(...), csrf_token: str = Form(""),
                   db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    a = db.query(Appointment).filter(Appointment.id == appt_id).first()
    if not a: return HTMLResponse("Not found", status_code=404)
    try:
        new_status = AppointmentStatus(status)
    except ValueError:
        return HTMLResponse("Invalid status value.", status_code=400)
    old_status = a.status
    a.status = new_status
    a.updated_at = datetime.utcnow()
    a.updated_by_user_id = user.id
    _audit(db, a.id, "status_changed", field_name="status",
           old_value=old_status.value if old_status else None, new_value=new_status.value)
    db.commit()
    if old_status != AppointmentStatus.cancelled and new_status == AppointmentStatus.cancelled:
        notify.send_waitlist_opening_notices(db, a)
    return RedirectResponse(f"/appointments/{appt_id}", status_code=303)

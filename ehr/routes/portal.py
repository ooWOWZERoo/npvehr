"""Online Patient Self-Booking (Calendar & Appointments UX Overhaul Phase 4
-- BUILD_BACKLOG.md 5a): a second, patient-facing application surface, with
its own passwordless (magic-link) auth (ehr/auth/portal_deps.py) entirely
separate from staff sessions.

Every route below is either public (the login flow) or gated on
get_current_patient -- never on ehr.auth.deps.get_current_user, and this
router is included directly on `app` in ehr/app.py with NO get_current_user
dependency, so a patient never needs (or gets) a staff session.

Scope this round (explicit decision): book, cancel, and reschedule, all
reusing the exact same conflict-rule engine
(ehr.routes.appointments._apply_scheduling_rules) staff booking uses -- a
patient can never override a conflict or a computed duration (those params
simply aren't exposed here), and relationship (new/established) is always
computed automatically, never patient-chosen. Reschedule keeps the same
provider and appointment type; only the time can change. Cancel and
reschedule both require at least PORTAL_SELF_SERVICE_CUTOFF_HOURS notice.
Only AppointmentTypeVersion rows with patient_bookable=True (default False,
an explicit per-type staff opt-in, see admin/scheduling/type_form.html) are
offered.
"""
from datetime import datetime, date, timedelta
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ehr.models.database import (get_db, Patient, Provider, AppointmentType, AppointmentTypeVersion,
    Appointment, AppointmentStatus)
from ehr.services import scheduling as sched
from ehr.services import notifications as notify
from ehr.env_info import EHR_ENV
from ehr.auth import csrf
from ehr.auth.portal_deps import (get_current_patient, issue_login_token, consume_login_token,
    create_portal_session, PORTAL_SESSION_COOKIE_NAME, PORTAL_SESSION_LIFETIME)
from ehr.routes.appointments import _apply_scheduling_rules, _sync_resource_reservations, _audit, _qi

router = APIRouter(prefix="/portal", tags=["portal"])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV

# A patient can't cancel/reschedule online inside this window before the
# appointment -- past this point they must call the office. Deliberately a
# simple module constant (not yet a per-practice setting) for this round.
PORTAL_SELF_SERVICE_CUTOFF_HOURS = 24


def _patient_bookable_type_versions(db: Session):
    """Mirrors appointments.py's _bookable_type_versions, filtered instead on
    patient_bookable (staff-opted-in for online booking) rather than
    staff_bookable -- the two flags are independent."""
    versions = (db.query(AppointmentTypeVersion)
                .join(AppointmentType, AppointmentType.id == AppointmentTypeVersion.appointment_type_id)
                .filter(AppointmentTypeVersion.active == True, AppointmentTypeVersion.patient_bookable == True,
                        AppointmentType.is_system_seeded == False)
                .order_by(AppointmentTypeVersion.display_order, AppointmentTypeVersion.appointment_type_id,
                          AppointmentTypeVersion.version_number.desc())
                .all())
    seen_type, result = set(), []
    for v in versions:
        if v.appointment_type_id in seen_type:
            continue
        seen_type.add(v.appointment_type_id)
        result.append(v)
    result.sort(key=lambda v: v.display_order)
    return result


# ---------------------------------------------------------------------------
# Login (public -- no get_current_patient dependency on any route below here)
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def portal_login_form(request: Request):
    # No portal session exists yet at this point, so this gets the same
    # short-lived double-submit-cookie CSRF token the staff /login uses
    # (ehr/auth/csrf.py's login-specific pair) rather than the session-bound
    # scheme every authenticated route below uses.
    login_csrf_token = csrf.generate_login_csrf()
    response = templates.TemplateResponse(request, "portal/login.html", {"login_csrf_token": login_csrf_token})
    response.set_cookie(csrf.LOGIN_CSRF_COOKIE_NAME, login_csrf_token, httponly=True,
                         secure=request.url.scheme == "https", samesite="lax", max_age=600)
    return response


@router.post("/login", response_class=HTMLResponse)
def portal_login_request(request: Request, email: str = Form(...), csrf_token: str = Form(""),
                          db: Session = Depends(get_db)):
    csrf.verify_login_csrf(request.cookies.get(csrf.LOGIN_CSRF_COOKIE_NAME), csrf_token)
    email = email.strip()
    matches = db.query(Patient).filter(Patient.email.isnot(None), Patient.email.ilike(email)).all() if email else []
    dev_links = []
    for patient in matches:
        token = issue_login_token(db, patient)
        db.flush()
        link_url = str(request.base_url).rstrip("/") + f"/portal/login/{token}"
        notify.send_portal_login_link(patient, link_url)
        if EHR_ENV != "production":
            dev_links.append({"patient": patient, "link_url": link_url})
    db.commit()
    # Same response whether or not `email` matched anyone -- never reveal
    # whether an address is on file (basic patient-enumeration hygiene).
    return templates.TemplateResponse(request, "portal/check_email.html", {"dev_links": dev_links})


@router.get("/login/{token}", response_class=HTMLResponse)
def portal_login_consume(request: Request, token: str, db: Session = Depends(get_db)):
    patient = consume_login_token(db, token)
    if not patient:
        db.commit()
        login_csrf_token = csrf.generate_login_csrf()
        response = templates.TemplateResponse(request, "portal/login.html", {
            "error": "That link is invalid or has expired. Request a new one below.",
            "login_csrf_token": login_csrf_token,
        }, status_code=400)
        response.set_cookie(csrf.LOGIN_CSRF_COOKIE_NAME, login_csrf_token, httponly=True,
                             secure=request.url.scheme == "https", samesite="lax", max_age=600)
        return response
    session_token = create_portal_session(db, patient)
    db.commit()
    response = RedirectResponse(url="/portal/", status_code=303)
    response.set_cookie(PORTAL_SESSION_COOKIE_NAME, session_token, httponly=True, samesite="lax",
                         max_age=int(PORTAL_SESSION_LIFETIME.total_seconds()))
    return response


@router.post("/logout")
def portal_logout(request: Request, patient: Patient = Depends(get_current_patient),
                   csrf_token: str = Form(""), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    token = request.cookies.get(PORTAL_SESSION_COOKIE_NAME)
    from ehr.models.database import PatientPortalSession
    sess = db.query(PatientPortalSession).filter(PatientPortalSession.session_token == token).first()
    if sess:
        sess.revoked = True
        db.commit()
    response = RedirectResponse(url="/portal/login", status_code=303)
    response.delete_cookie(PORTAL_SESSION_COOKIE_NAME)
    return response


# ---------------------------------------------------------------------------
# Authenticated portal (every route below requires get_current_patient)
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def portal_dashboard(request: Request, patient: Patient = Depends(get_current_patient), db: Session = Depends(get_db)):
    now = datetime.utcnow()
    upcoming = (db.query(Appointment)
                .filter(Appointment.patient_id == patient.id, Appointment.scheduled_at >= now,
                        Appointment.status == AppointmentStatus.scheduled)
                .order_by(Appointment.scheduled_at).all())
    return templates.TemplateResponse(request, "portal/dashboard.html", {"patient": patient, "upcoming": upcoming})


def _slot_search_context(db: Session, provider_id, appointment_type_version_id, date_str, patient_id,
                          exclude_appointment_id=None):
    providers = db.query(Provider).all()
    types = _patient_bookable_type_versions(db)
    target_date = date.fromisoformat(date_str) if date_str else date.today()
    slots, version, error = [], None, None
    if provider_id and appointment_type_version_id:
        version = (db.query(AppointmentTypeVersion)
                   .filter(AppointmentTypeVersion.id == appointment_type_version_id,
                           AppointmentTypeVersion.patient_bookable == True).first())
        if not version or not version.active:
            error = "Selected appointment type is not available for online booking."
        else:
            relationship = sched.suggest_relationship(db, patient_id, target_date.isoformat())
            if not sched.eligible_for_relationship(version, relationship):
                error = (f"{version.display_name} isn't available to book online for your patient status. "
                          f"Please call the office.")
            else:
                duration_minutes = sched.compute_duration_minutes(version, relationship)
                slots = sched.find_open_slots(db, provider_id, target_date, duration_minutes,
                                               exclude_appointment_id=exclude_appointment_id)
    return {"providers": providers, "types": types, "provider_id": provider_id,
            "appointment_type_version_id": appointment_type_version_id, "target_date": target_date,
            "slots": slots, "version": version, "error": error}


@router.get("/book", response_class=HTMLResponse)
def portal_book_search(request: Request, provider_id: str = None, appointment_type_version_id: str = None,
                        date_str: str = None, patient: Patient = Depends(get_current_patient),
                        db: Session = Depends(get_db)):
    # provider_id/appointment_type_version_id come from <select>s whose empty-
    # value "Choose a..." option submits "" (not absent) once the form is
    # resubmitted onchange -- a plain `int = None` param 422s on that, per the
    # same pre-existing bug documented on the staff board's filters
    # (BUILD_BACKLOG.md 5a Phase 1). _qi() treats "" the same as absent.
    ctx = _slot_search_context(db, _qi(provider_id), _qi(appointment_type_version_id), date_str, patient.id)
    return templates.TemplateResponse(request, "portal/book.html", ctx)


@router.post("/book/confirm", response_class=HTMLResponse)
def portal_book_confirm(request: Request, provider_id: int = Form(...),
    appointment_type_version_id: int = Form(...), scheduled_at: str = Form(...),
    csrf_token: str = Form(""), patient: Patient = Depends(get_current_patient), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    version = (db.query(AppointmentTypeVersion)
               .filter(AppointmentTypeVersion.id == appointment_type_version_id,
                       AppointmentTypeVersion.patient_bookable == True, AppointmentTypeVersion.active == True)
               .first())
    try:
        when = datetime.fromisoformat(scheduled_at)
    except (ValueError, TypeError):
        version = None  # fall through to the generic error below
    if not version:
        ctx = _slot_search_context(db, provider_id, appointment_type_version_id, None, patient.id)
        ctx["error"] = "That appointment type is no longer available online. Please choose another."
        return templates.TemplateResponse(request, "portal/book.html", ctx, status_code=400)

    relationship = sched.suggest_relationship(db, patient.id, when.date().isoformat())
    appt = Appointment(patient_id=patient.id, status=AppointmentStatus.scheduled)
    try:
        _apply_scheduling_rules(db, appt, version, relationship, False, when, provider_id)
    except ValueError:
        ctx = _slot_search_context(db, provider_id, appointment_type_version_id, when.date().isoformat(), patient.id)
        ctx["error"] = "That time was just booked by someone else. Please choose another slot."
        return templates.TemplateResponse(request, "portal/book.html", ctx, status_code=409)

    db.add(appt); db.flush()
    _sync_resource_reservations(db, appt, version)
    _audit(db, appt.id, "created", reason="Booked via patient portal")
    db.commit(); db.refresh(appt)
    notify.send_appointment_notice(db, appt, "confirmation")
    return RedirectResponse("/portal/appointments?booked=1", status_code=303)


@router.get("/appointments", response_class=HTMLResponse)
def portal_appointments(request: Request, booked: str = None, patient: Patient = Depends(get_current_patient),
                         db: Session = Depends(get_db)):
    appts = (db.query(Appointment).filter(Appointment.patient_id == patient.id)
             .order_by(Appointment.scheduled_at.desc()).all())
    now = datetime.utcnow()
    cutoff = timedelta(hours=PORTAL_SELF_SERVICE_CUTOFF_HOURS)
    return templates.TemplateResponse(request, "portal/appointments.html", {
        "appointments": appts, "now": now, "cutoff": cutoff, "booked": bool(booked)})


def _own_scheduled_appointment_or_none(db: Session, patient: Patient, appt_id: int):
    """Ownership check: an appointment id belonging to another patient (or
    not found, or not in a cancellable/reschedulable state) returns None --
    the caller responds 404 either way, never revealing which case it was."""
    appt = db.query(Appointment).filter(Appointment.id == appt_id).first()
    if not appt or appt.patient_id != patient.id or appt.status != AppointmentStatus.scheduled:
        return None
    return appt


@router.post("/appointments/{appt_id}/cancel")
def portal_cancel_appointment(request: Request, appt_id: int, csrf_token: str = Form(""),
                               patient: Patient = Depends(get_current_patient), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    appt = _own_scheduled_appointment_or_none(db, patient, appt_id)
    if not appt:
        return HTMLResponse("Not found", status_code=404)
    if appt.scheduled_at - datetime.utcnow() < timedelta(hours=PORTAL_SELF_SERVICE_CUTOFF_HOURS):
        return HTMLResponse(
            f"This appointment is within {PORTAL_SELF_SERVICE_CUTOFF_HOURS} hours and can no longer be "
            f"cancelled online -- please call the office.", status_code=400)
    appt.status = AppointmentStatus.cancelled
    _audit(db, appt.id, "status_changed", field_name="status", old_value="scheduled", new_value="cancelled",
           reason="Cancelled via patient portal")
    db.commit()
    return RedirectResponse("/portal/appointments?cancelled=1", status_code=303)


@router.get("/appointments/{appt_id}/reschedule", response_class=HTMLResponse)
def portal_reschedule_search(request: Request, appt_id: int, date_str: str = None,
                              patient: Patient = Depends(get_current_patient), db: Session = Depends(get_db)):
    appt = _own_scheduled_appointment_or_none(db, patient, appt_id)
    if not appt:
        return HTMLResponse("Not found", status_code=404)
    if appt.scheduled_at - datetime.utcnow() < timedelta(hours=PORTAL_SELF_SERVICE_CUTOFF_HOURS):
        return HTMLResponse(
            f"This appointment is within {PORTAL_SELF_SERVICE_CUTOFF_HOURS} hours and can no longer be "
            f"rescheduled online -- please call the office.", status_code=400)
    ctx = _slot_search_context(db, appt.provider_id, appt.appointment_type_version_id,
                                date_str or appt.scheduled_at.date().isoformat(), patient.id,
                                exclude_appointment_id=appt.id)
    ctx["appt"] = appt
    return templates.TemplateResponse(request, "portal/reschedule.html", ctx)


@router.post("/appointments/{appt_id}/reschedule/confirm")
def portal_reschedule_confirm(request: Request, appt_id: int, scheduled_at: str = Form(...),
                               csrf_token: str = Form(""), patient: Patient = Depends(get_current_patient),
                               db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    appt = _own_scheduled_appointment_or_none(db, patient, appt_id)
    if not appt:
        return HTMLResponse("Not found", status_code=404)
    if appt.scheduled_at - datetime.utcnow() < timedelta(hours=PORTAL_SELF_SERVICE_CUTOFF_HOURS):
        return HTMLResponse(
            f"This appointment is within {PORTAL_SELF_SERVICE_CUTOFF_HOURS} hours and can no longer be "
            f"rescheduled online -- please call the office.", status_code=400)
    try:
        when = datetime.fromisoformat(scheduled_at)
    except (ValueError, TypeError):
        return HTMLResponse("Invalid date/time.", status_code=400)

    version = appt.appointment_type_version
    old_when = appt.scheduled_at
    try:
        _apply_scheduling_rules(db, appt, version, appt.patient_relationship_at_booking, appt.is_follow_up,
            when, appt.provider_id, exclude_appointment_id=appt.id)
    except ValueError:
        ctx = _slot_search_context(db, appt.provider_id, appt.appointment_type_version_id,
                                    when.date().isoformat(), patient.id, exclude_appointment_id=appt.id)
        ctx["appt"] = appt
        ctx["error"] = "That time was just booked by someone else. Please choose another slot."
        return templates.TemplateResponse(request, "portal/reschedule.html", ctx, status_code=409)

    _sync_resource_reservations(db, appt, version)
    _audit(db, appt.id, "rescheduled", field_name="scheduled_at", old_value=old_when, new_value=when,
           reason="Rescheduled via patient portal")
    db.commit()
    return RedirectResponse("/portal/appointments?rescheduled=1", status_code=303)

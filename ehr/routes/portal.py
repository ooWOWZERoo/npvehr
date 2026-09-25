"""Online Patient Self-Booking (Calendar & Appointments UX Overhaul Phase 4
-- BUILD_BACKLOG.md 5a): a second, patient-facing application surface, with
its own passwordless (magic-link) auth (ehr/auth/portal_deps.py) entirely
separate from staff sessions.

Every route below is either public (the login flow) or gated on
get_current_patient -- never on ehr.auth.deps.get_current_user, and this
router is included directly on `app` in ehr/app.py with NO get_current_user
dependency, so a patient never needs (or gets) a staff session.

Scope originally (Phase 4): book, cancel, and reschedule, all reusing the
exact same conflict-rule engine (ehr.routes.appointments.
_apply_scheduling_rules) staff booking uses -- a patient can never override
a conflict or a computed duration (those params simply aren't exposed
here), and relationship (new/established) is always computed automatically,
never patient-chosen. Cancel and reschedule both require at least the
configurable PortalSettings.self_service_cutoff_hours notice (admin/
scheduling/portal_settings.html). Only AppointmentTypeVersion rows with
patient_bookable=True (default False, an explicit per-type staff opt-in,
see admin/scheduling/type_form.html) are offered.

Phase 4 follow-ups added in this round (BUILD_BACKLOG.md 5a): reschedule
can now also change provider/appointment type, not just time; the cutoff
window is a staff-configurable setting instead of a hardcoded constant;
login-link requests are rate-limited per matched patient; a patient can
join/view/cancel their own waitlist entries; and a patient-facing view of
their own visit summaries, prescriptions, and documents (each view logged
to PortalAccessAuditEvent for staff visibility). Explicitly still out of
scope: patient self-registration (the portal only authenticates existing
chart-matched emails) and a real email vendor (magic links are still
mocked/logged, per Phase 3).
"""
from datetime import datetime, date, timedelta
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ehr.models.database import (get_db, Patient, AppointmentType, AppointmentTypeVersion,
    Appointment, AppointmentStatus, PortalSettings, PatientPortalLoginToken, WaitlistEntry, EyeExam,
    Prescription, PatientDocument, PortalAccessAuditEvent)
from ehr.services import scheduling as sched
from ehr.services import notifications as notify
from ehr.services.media import get_document_bytes
from ehr.env_info import EHR_ENV
from ehr.auth import csrf
from ehr.auth.portal_deps import (get_current_patient, issue_login_token, consume_login_token,
    create_portal_session, PORTAL_SESSION_COOKIE_NAME, PORTAL_SESSION_LIFETIME)
from ehr.routes.appointments import _apply_scheduling_rules, _sync_resource_reservations, _audit, _qi

router = APIRouter(prefix="/portal", tags=["portal"])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV

# Login-link request rate limit: at most this many tokens minted per matched
# patient within the trailing window below, regardless of how many times
# their email is submitted -- prevents /portal/login being used to spam a
# patient's inbox (once a real vendor is wired up) or hammer the DB with
# token rows. No new schema needed: counts existing PatientPortalLoginToken
# rows rather than tracking attempts separately.
LOGIN_LINK_RATE_LIMIT_COUNT = 3
LOGIN_LINK_RATE_LIMIT_WINDOW = timedelta(minutes=15)


def _get_cutoff_hours(db: Session) -> int:
    """The patient self-service cutoff window, now a staff-configurable
    setting (admin/scheduling/portal_settings.html) rather than a hardcoded
    constant. Falls back to 24 if the singleton settings row is somehow
    missing (shouldn't happen post-migration)."""
    settings = db.query(PortalSettings).filter(PortalSettings.id == 1).first()
    return settings.self_service_cutoff_hours if settings else 24


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
    since = datetime.utcnow() - LOGIN_LINK_RATE_LIMIT_WINDOW
    for patient in matches:
        # Rate limit per matched patient, silently -- the response is
        # identical either way (see the comment below), so a rate-limited
        # request looks exactly like a normal one to whoever's asking.
        recent_count = (db.query(PatientPortalLoginToken)
                        .filter(PatientPortalLoginToken.patient_id == patient.id,
                                PatientPortalLoginToken.created_at >= since)
                        .count())
        if recent_count >= LOGIN_LINK_RATE_LIMIT_COUNT:
            continue
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


@router.get("/register", response_class=HTMLResponse)
def portal_register_form(request: Request):
    """Patient Self-Registration (BUILD_BACKLOG.md 5a follow-up): the portal
    previously only authenticated existing chart-matched emails, with no way
    for someone with no chart at all to get one. Same passwordless posture as
    /portal/login -- there is no password to set here either, just a form
    that (per the POST handler below) always ends in the same magic-link
    email-confirmation step /portal/login already uses, so identity
    verification and account activation are the same single step."""
    login_csrf_token = csrf.generate_login_csrf()
    response = templates.TemplateResponse(request, "portal/register.html", {"login_csrf_token": login_csrf_token})
    response.set_cookie(csrf.LOGIN_CSRF_COOKIE_NAME, login_csrf_token, httponly=True,
                         secure=request.url.scheme == "https", samesite="lax", max_age=600)
    return response


@router.post("/register", response_class=HTMLResponse)
def portal_register_submit(request: Request, first_name: str = Form(...), last_name: str = Form(...),
    date_of_birth: str = Form(""), email: str = Form(...), phone: str = Form(""),
    csrf_token: str = Form(""), db: Session = Depends(get_db)):
    csrf.verify_login_csrf(request.cookies.get(csrf.LOGIN_CSRF_COOKIE_NAME), csrf_token)
    first_name, last_name, email, phone = first_name.strip(), last_name.strip(), email.strip(), phone.strip()
    posted = {"first_name": first_name, "last_name": last_name, "date_of_birth": date_of_birth, "email": email, "phone": phone}
    if not first_name or not last_name or not email:
        login_csrf_token = csrf.generate_login_csrf()
        response = templates.TemplateResponse(request, "portal/register.html", {
            "error": "First name, last name, and email are all required.",
            "posted": posted, "login_csrf_token": login_csrf_token,
        }, status_code=400)
        response.set_cookie(csrf.LOGIN_CSRF_COOKIE_NAME, login_csrf_token, httponly=True,
                             secure=request.url.scheme == "https", samesite="lax", max_age=600)
        return response

    # An email that already matches an existing chart signs that patient in
    # rather than creating a duplicate one -- and, same as /portal/login,
    # the response is identical either way, so this page never reveals
    # whether an address was already on file.
    patient = db.query(Patient).filter(Patient.email.isnot(None), Patient.email.ilike(email)).first()
    if not patient:
        patient = Patient(first_name=first_name, last_name=last_name, date_of_birth=date_of_birth or None,
            email=email, phone=phone or None, self_registered_at=datetime.utcnow())
        db.add(patient)
        db.flush()

    dev_links = []
    token = issue_login_token(db, patient)
    db.flush()
    link_url = str(request.base_url).rstrip("/") + f"/portal/login/{token}"
    notify.send_portal_login_link(patient, link_url)
    if EHR_ENV != "production":
        dev_links.append({"patient": patient, "link_url": link_url})
    db.commit()
    return templates.TemplateResponse(request, "portal/check_email.html", {"dev_links": dev_links, "registered": True})


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
                          exclude_appointment_id=None, include_provider_id=None):
    providers = sched.bookable_providers(db, include_id=include_provider_id)
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
                                               version=version, exclude_appointment_id=exclude_appointment_id)
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
    cutoff = timedelta(hours=_get_cutoff_hours(db))
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
    cutoff_hours = _get_cutoff_hours(db)
    if appt.scheduled_at - datetime.utcnow() < timedelta(hours=cutoff_hours):
        return HTMLResponse(
            f"This appointment is within {cutoff_hours} hours and can no longer be "
            f"cancelled online -- please call the office.", status_code=400)
    appt.status = AppointmentStatus.cancelled
    _audit(db, appt.id, "status_changed", field_name="status", old_value="scheduled", new_value="cancelled",
           reason="Cancelled via patient portal")
    db.commit()
    notify.send_waitlist_opening_notices(db, appt)
    return RedirectResponse("/portal/appointments?cancelled=1", status_code=303)


@router.get("/appointments/{appt_id}/reschedule", response_class=HTMLResponse)
def portal_reschedule_search(request: Request, appt_id: int, provider_id: str = None,
                              appointment_type_version_id: str = None, date_str: str = None,
                              patient: Patient = Depends(get_current_patient), db: Session = Depends(get_db)):
    appt = _own_scheduled_appointment_or_none(db, patient, appt_id)
    if not appt:
        return HTMLResponse("Not found", status_code=404)
    cutoff_hours = _get_cutoff_hours(db)
    if appt.scheduled_at - datetime.utcnow() < timedelta(hours=cutoff_hours):
        return HTMLResponse(
            f"This appointment is within {cutoff_hours} hours and can no longer be "
            f"rescheduled online -- please call the office.", status_code=400)
    # Phase 4 follow-up: provider/type can now change on a self-service
    # reschedule too, not just the time -- defaults to the appointment's
    # current provider/type when the patient hasn't picked different ones.
    ctx = _slot_search_context(db, _qi(provider_id) or appt.provider_id,
                                _qi(appointment_type_version_id) or appt.appointment_type_version_id,
                                date_str or appt.scheduled_at.date().isoformat(), patient.id,
                                exclude_appointment_id=appt.id, include_provider_id=appt.provider_id)
    ctx["appt"] = appt
    return templates.TemplateResponse(request, "portal/reschedule.html", ctx)


@router.post("/appointments/{appt_id}/reschedule/confirm")
def portal_reschedule_confirm(request: Request, appt_id: int, provider_id: int = Form(...),
                               appointment_type_version_id: int = Form(...), scheduled_at: str = Form(...),
                               csrf_token: str = Form(""), patient: Patient = Depends(get_current_patient),
                               db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    appt = _own_scheduled_appointment_or_none(db, patient, appt_id)
    if not appt:
        return HTMLResponse("Not found", status_code=404)
    cutoff_hours = _get_cutoff_hours(db)
    if appt.scheduled_at - datetime.utcnow() < timedelta(hours=cutoff_hours):
        return HTMLResponse(
            f"This appointment is within {cutoff_hours} hours and can no longer be "
            f"rescheduled online -- please call the office.", status_code=400)
    try:
        when = datetime.fromisoformat(scheduled_at)
    except (ValueError, TypeError):
        when = None

    version = (db.query(AppointmentTypeVersion)
               .filter(AppointmentTypeVersion.id == appointment_type_version_id,
                       AppointmentTypeVersion.patient_bookable == True, AppointmentTypeVersion.active == True)
               .first())

    def fail(msg, status_code):
        ctx = _slot_search_context(db, provider_id, appointment_type_version_id,
                                    (when or appt.scheduled_at).date().isoformat(), patient.id,
                                    exclude_appointment_id=appt.id, include_provider_id=appt.provider_id)
        ctx["appt"] = appt
        ctx["error"] = msg
        return templates.TemplateResponse(request, "portal/reschedule.html", ctx, status_code=status_code)

    if when is None:
        return fail("Invalid date/time.", 400)
    if not version:
        return fail("That appointment type is no longer available online. Please choose another.", 400)

    relationship = sched.suggest_relationship(db, patient.id, when.date().isoformat())
    if not sched.eligible_for_relationship(version, relationship):
        return fail(f"{version.display_name} isn't available to book online for your patient status. "
                    f"Please call the office.", 400)

    old_when = appt.scheduled_at
    try:
        _apply_scheduling_rules(db, appt, version, relationship, appt.is_follow_up,
            when, provider_id, exclude_appointment_id=appt.id)
    except ValueError:
        return fail("That time was just booked by someone else. Please choose another slot.", 409)

    _sync_resource_reservations(db, appt, version)
    _audit(db, appt.id, "rescheduled", field_name="scheduled_at", old_value=old_when, new_value=when,
           reason="Rescheduled via patient portal")
    db.commit()
    return RedirectResponse("/portal/appointments?rescheduled=1", status_code=303)


# ---------------------------------------------------------------------------
# Waitlist self-service (Phase 4 follow-up, BUILD_BACKLOG.md 5a): a patient
# can join or view their own WaitlistEntry rows -- the same model and
# matching logic (ehr.services.scheduling.find_matching_waitlist_entries)
# staff already use, just a second entry point alongside patients/waitlist_tab.html.
# ---------------------------------------------------------------------------

@router.get("/waitlist", response_class=HTMLResponse)
def portal_waitlist(request: Request, patient: Patient = Depends(get_current_patient), db: Session = Depends(get_db)):
    entries = (db.query(WaitlistEntry).filter(WaitlistEntry.patient_id == patient.id)
               .order_by(WaitlistEntry.status, WaitlistEntry.created_at.desc()).all())
    providers = sched.bookable_providers(db)
    types = _patient_bookable_type_versions(db)
    return templates.TemplateResponse(request, "portal/waitlist.html",
        {"entries": entries, "providers": providers, "types": types})


@router.post("/waitlist/new")
def portal_waitlist_new(request: Request, provider_id: str = Form(""), appointment_type_version_id: str = Form(""),
    desired_date_start: str = Form(""), desired_date_end: str = Form(""), notes: str = Form(""),
    csrf_token: str = Form(""), patient: Patient = Depends(get_current_patient), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    # No priority field here -- unlike the staff-facing form (patients/
    # waitlist_tab.html), "urgent" is a triage judgment call this app leaves
    # to staff; a patient-created entry always starts "normal" (staff can
    # still re-triage it, same as any other entry, via direct DB access or
    # a future admin affordance -- there is no edit route for priority today).
    db.add(WaitlistEntry(
        patient_id=patient.id,
        provider_id=_qi(provider_id),
        appointment_type_version_id=_qi(appointment_type_version_id),
        desired_date_start=desired_date_start or None,
        desired_date_end=desired_date_end or None,
        priority="normal",
        notes=notes or None))
    db.commit()
    return RedirectResponse("/portal/waitlist", status_code=303)


@router.post("/waitlist/{entry_id}/cancel")
def portal_waitlist_cancel(request: Request, entry_id: int, csrf_token: str = Form(""),
                            patient: Patient = Depends(get_current_patient), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    entry = db.query(WaitlistEntry).filter(WaitlistEntry.id == entry_id, WaitlistEntry.patient_id == patient.id).first()
    if not entry:
        return HTMLResponse("Not found", status_code=404)
    if entry.status == "active":
        entry.status = "cancelled"
        entry.cancelled_at = datetime.utcnow()
        db.commit()
    return RedirectResponse("/portal/waitlist", status_code=303)


# ---------------------------------------------------------------------------
# Patient-facing clinical data view (Phase 4 follow-up, BUILD_BACKLOG.md 5a):
# read-only visit summaries, prescriptions, and documents, scoped strictly
# to the logged-in patient's own records (every route re-checks ownership,
# 404 on any mismatch, same posture as the appointment routes above). Every
# view is logged to PortalAccessAuditEvent for staff visibility into when a
# patient looked at their own data.
# ---------------------------------------------------------------------------

def _log_portal_access(db: Session, patient_id: int, resource_type: str, resource_id: int = None):
    db.add(PortalAccessAuditEvent(patient_id=patient_id, resource_type=resource_type, resource_id=resource_id))
    db.commit()


@router.get("/records", response_class=HTMLResponse)
def portal_records_home(request: Request, patient: Patient = Depends(get_current_patient)):
    return templates.TemplateResponse(request, "portal/records_home.html", {})


@router.get("/records/visits", response_class=HTMLResponse)
def portal_records_visits(request: Request, patient: Patient = Depends(get_current_patient), db: Session = Depends(get_db)):
    exams = (db.query(EyeExam).filter(EyeExam.patient_id == patient.id)
             .order_by(EyeExam.exam_date.desc()).all())
    return templates.TemplateResponse(request, "portal/records_visits.html", {"exams": exams})


@router.get("/records/visits/{exam_id}", response_class=HTMLResponse)
def portal_records_visit_detail(request: Request, exam_id: int, patient: Patient = Depends(get_current_patient),
                                 db: Session = Depends(get_db)):
    exam = db.query(EyeExam).filter(EyeExam.id == exam_id).first()
    if not exam or exam.patient_id != patient.id:
        return HTMLResponse("Not found", status_code=404)
    _log_portal_access(db, patient.id, "visit_summary", exam.id)
    return templates.TemplateResponse(request, "portal/records_visit_detail.html", {"exam": exam})


@router.get("/records/prescriptions", response_class=HTMLResponse)
def portal_records_prescriptions(request: Request, patient: Patient = Depends(get_current_patient),
                                  db: Session = Depends(get_db)):
    rxs = (db.query(Prescription).filter(Prescription.patient_id == patient.id)
           .order_by(Prescription.issue_date.desc()).all())
    _log_portal_access(db, patient.id, "prescriptions")
    return templates.TemplateResponse(request, "portal/records_prescriptions.html", {"prescriptions": rxs})


@router.get("/records/documents", response_class=HTMLResponse)
def portal_records_documents(request: Request, patient: Patient = Depends(get_current_patient),
                              db: Session = Depends(get_db)):
    docs = (db.query(PatientDocument).filter(PatientDocument.patient_id == patient.id)
            .order_by(PatientDocument.uploaded_at.desc()).all())
    return templates.TemplateResponse(request, "portal/records_documents.html", {"documents": docs})


@router.get("/records/documents/{doc_id}")
def portal_records_document_download(doc_id: int, patient: Patient = Depends(get_current_patient),
                                      db: Session = Depends(get_db)):
    """Same secure-proxy pattern as the staff download route (ehr.routes.
    patients.download_patient_document): the document is only ever served
    if its own patient_id matches the logged-in patient -- a valid doc_id
    for another patient's document 404s here, never revealing it exists."""
    doc = db.query(PatientDocument).filter(PatientDocument.id == doc_id).first()
    if not doc or doc.patient_id != patient.id:
        return HTMLResponse(status_code=404, content="")
    result = get_document_bytes(doc.storage_marker)
    if result is None:
        return HTMLResponse(status_code=404, content="")
    data, content_type = result
    _log_portal_access(db, patient.id, "document", doc.id)
    return Response(content=data, media_type=doc.content_type or content_type,
                     headers={"Cache-Control": "private, max-age=300",
                              "Content-Disposition": f'inline; filename="{doc.original_filename}"'})

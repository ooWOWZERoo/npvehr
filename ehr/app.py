from fastapi import FastAPI, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime
from ehr.models.database import init_db, engine, get_db, Patient, Appointment, EyeExam, Prescription, AppointmentStatus
from ehr.db.migrations import run_column_migrations, run_post_create_all_migrations
from ehr.env_info import EHR_ENV
from ehr.routes import patients, appointments, exams, prescriptions, admin_scheduling, store_ops, auth as auth_routes
from ehr.auth.deps import get_current_user, LoginRedirect
from ehr.auth.permissions import (ROLE_LABELS, ANY_STAFF, PATIENT_EDIT, APPOINTMENT_EDIT, EXAM_VIEW, EXAM_EDIT,
    RX_VIEW, RX_EDIT, ADMIN_SCHEDULING_VIEW, ADMIN_SCHEDULING_EDIT, STORE_OPS_VIEW, STORE_OPS_EDIT, CLAIMS_VIEW,
    CATALOG_ORDERS_VIEW, require_role)

# /docs, /redoc, /openapi.json disabled entirely (spec: these were a known
# publicly-exposed gap). Simpler and more airtight than gating FastAPI's
# auto-generated docs UI behind auth, and this app has no external API
# consumers who would need them.
app = FastAPI(title="New Path Vision EHR", version="1.0.0", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory="ehr/static"), name="static")
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS

@app.exception_handler(LoginRedirect)
async def _login_redirect_handler(request: Request, exc: LoginRedirect):
    return exc.response

app.include_router(auth_routes.router)

# Every route in these 6 pre-existing route files now requires a valid,
# non-expired, non-revoked session (get_current_user) -- applied here at
# router-inclusion time rather than editing every individual route function,
# so coverage is total and cannot silently miss a route added later to any
# of these files. Role-specific restrictions beyond "any authenticated
# staff member" are layered on top per-route via require_role(...) below,
# matching the group constants in ehr/auth/permissions.py.
app.include_router(patients.router, dependencies=[Depends(get_current_user)])
app.include_router(appointments.router, dependencies=[Depends(get_current_user)])
app.include_router(exams.router, dependencies=[Depends(get_current_user)])
app.include_router(prescriptions.router, dependencies=[Depends(get_current_user)])
app.include_router(admin_scheduling.router, dependencies=[Depends(get_current_user)])
app.include_router(store_ops.router, dependencies=[Depends(get_current_user)])

@app.on_event("startup")
def startup():
    # Migration ordering (spec 22.1): ALTER TABLE on existing tables first, then
    # create_all() for brand-new tables, then data-seed/backfill migrations that
    # depend on those new tables existing. Safe to run on every startup.
    run_column_migrations(engine)
    init_db()
    run_post_create_all_migrations(engine)

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    now = datetime.utcnow()
    upcoming = (db.query(Appointment)
                .filter(Appointment.scheduled_at >= now, Appointment.status == AppointmentStatus.scheduled)
                .order_by(Appointment.scheduled_at).limit(5).all())
    # Reusable alert banner, demo call site #2 (see ehr/templates/_alert_banner.html):
    # appointments today that are still 'scheduled' even though their start time has
    # already passed -- likely no-shows or forgotten check-ins.
    today_start = datetime(now.year, now.month, now.day)
    overdue_scheduled_count = (db.query(Appointment)
        .filter(Appointment.scheduled_at >= today_start, Appointment.scheduled_at < now,
                Appointment.status == AppointmentStatus.scheduled)
        .count())
    return templates.TemplateResponse(request, "dashboard.html", {
        "total_patients": db.query(Patient).count(),
        "total_appointments": db.query(Appointment).count(),
        "total_exams": db.query(EyeExam).count(),
        "total_rx": db.query(Prescription).count(),
        "upcoming": upcoming,
        "recent_patients": db.query(Patient).order_by(Patient.created_at.desc()).limit(5).all(),
        "overdue_scheduled_count": overdue_scheduled_count,
    })

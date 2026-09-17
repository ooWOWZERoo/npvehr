"""Patient self-service portal authentication (Calendar & Appointments UX
Overhaul Phase 4 -- BUILD_BACKLOG.md 5a). Deliberately a separate system from
ehr/auth/deps.py's staff session auth -- distinct cookie name, distinct
session table (PatientPortalSession), no shared code path -- so a patient's
login and a staff member's login never collide in the same browser, and a
bug in one auth system can't silently grant access via the other.

Login is passwordless (a magic link): the patient enters their email at
/portal/login, the server looks up matching Patient rows and mints a
PatientPortalLoginToken per match, and ehr.services.notifications "sends"
(mocks/logs -- no real email vendor this round, same posture as Phase 3's
AppointmentReminder) a link to /portal/login/<token>. Visiting that link
consumes the token and creates a PatientPortalSession, cookie-based exactly
like the staff scheme (see get_current_patient below).
"""
import secrets
from datetime import datetime, timedelta
from fastapi import Request, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from ehr.models.database import get_db, Patient, PatientPortalSession, PatientPortalLoginToken
from ehr.auth.csrf import generate_csrf_token

PORTAL_SESSION_COOKIE_NAME = "npv_portal_session"
PORTAL_SESSION_LIFETIME = timedelta(hours=12)
LOGIN_TOKEN_LIFETIME = timedelta(minutes=15)


class PortalLoginRedirect(Exception):
    """Raised by get_current_patient to short-circuit an unauthenticated
    request straight to a 303 redirect to /portal/login -- the patient-portal
    equivalent of ehr.auth.deps.LoginRedirect. Caught by the same kind of
    exception handler, registered separately in ehr/app.py."""
    def __init__(self, response: RedirectResponse):
        self.response = response


def issue_login_token(db: Session, patient: Patient) -> str:
    token = secrets.token_urlsafe(32)
    db.add(PatientPortalLoginToken(patient_id=patient.id, token=token,
        expires_at=datetime.utcnow() + LOGIN_TOKEN_LIFETIME))
    return token


def consume_login_token(db: Session, token: str):
    """Returns the Patient if `token` is a valid, unused, unexpired login
    token (and marks it used) -- otherwise None. Single-use: a used_at
    already set (even from a moment ago) fails, so a magic link cannot be
    replayed."""
    row = db.query(PatientPortalLoginToken).filter(PatientPortalLoginToken.token == token).first()
    if not row or row.used_at or row.expires_at < datetime.utcnow():
        return None
    row.used_at = datetime.utcnow()
    return db.query(Patient).filter(Patient.id == row.patient_id).first()


def create_portal_session(db: Session, patient: Patient) -> str:
    token = secrets.token_urlsafe(32)
    db.add(PatientPortalSession(patient_id=patient.id, session_token=token,
        expires_at=datetime.utcnow() + PORTAL_SESSION_LIFETIME))
    return token


def _load_valid_session(db: Session, token: str):
    if not token:
        return None, None
    sess = db.query(PatientPortalSession).filter(PatientPortalSession.session_token == token).first()
    if not sess or sess.revoked:
        return None, None
    now = datetime.utcnow()
    if sess.expires_at and now > sess.expires_at:
        sess.revoked = True
        db.commit()
        return None, None
    patient = db.query(Patient).filter(Patient.id == sess.patient_id).first()
    if not patient:
        return None, None
    return sess, patient


def get_current_patient(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(PORTAL_SESSION_COOKIE_NAME)
    sess, patient = _load_valid_session(db, token)
    if not patient:
        response = RedirectResponse(url="/portal/login", status_code=303)
        raise PortalLoginRedirect(response)
    sess.last_seen_at = datetime.utcnow()
    db.commit()
    request.state.patient = patient
    request.state.csrf_token = generate_csrf_token(sess.session_token)
    return patient

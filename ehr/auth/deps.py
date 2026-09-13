"""Session-cookie authentication dependency (get_current_user), applied to
essentially every existing route -- at router-inclusion time in ehr/app.py
for the 6 pre-existing route files, and directly on the dashboard route and
the /admin/* routes defined in ehr/app.py / ehr/routes/auth.py.

Session mechanism (documented per spec):
  - Cookie holds the raw session_token value (see ehr/models/database.py's
    UserSession.session_token, a secrets.token_urlsafe(32) string) directly,
    stored in the DB AS-IS with a unique index rather than hashed-at-rest.
    Tradeoff, chosen deliberately: the token already carries 256 bits of
    entropy (effectively unguessable), and hashing it at rest would only
    matter against a threat model where the sessions table leaks independently
    of the rest of this single-file SQLite database -- not a scenario this
    local app's threat model needs to defend against beyond what password
    hashing (which DOES protect against full-DB compromise) already covers.
  - Hard expiry: SESSION_LIFETIME (12 hours) from creation, fixed at issuance
    -- not extended by activity ("sliding" was considered and rejected in
    favor of a simple, easy-to-reason-about hard cap).
  - Inactivity timeout: INACTIVITY_TIMEOUT (30 minutes). Every authenticated
    request updates last_seen_at; a request arriving more than 30 minutes
    after the session's own last_seen_at is rejected and the session is
    revoked server-side, even if the 12-hour hard expiry has not been reached.
  - Logout (ehr/routes/auth.py) sets revoked=True immediately -- this is what
    actually invalidates the session; clearing the browser cookie is a
    courtesy on top of that, not the mechanism itself.
"""
from datetime import datetime, timedelta
from fastapi import Request, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from ehr.models.database import get_db, User, UserSession
from ehr.auth.csrf import generate_csrf_token

SESSION_COOKIE_NAME = "npv_session"
SESSION_LIFETIME = timedelta(hours=12)
INACTIVITY_TIMEOUT = timedelta(minutes=30)


class LoginRedirect(Exception):
    """Raised by get_current_user to short-circuit an unauthenticated request
    straight to a 303 redirect to /login?next=<original path>. Caught by an
    exception handler registered in ehr/app.py -- this is the standard FastAPI
    pattern for a dependency to "return" a response other than raising an
    HTTPException."""
    def __init__(self, response: RedirectResponse):
        self.response = response


def _load_valid_session(db: Session, token: str):
    if not token:
        return None, None
    sess = db.query(UserSession).filter(UserSession.session_token == token).first()
    if not sess or sess.revoked:
        return None, None
    now = datetime.utcnow()
    if sess.expires_at and now > sess.expires_at:
        sess.revoked = True
        db.commit()
        return None, None
    if sess.last_seen_at and (now - sess.last_seen_at) > INACTIVITY_TIMEOUT:
        sess.revoked = True
        db.commit()
        return None, None
    user = db.query(User).filter(User.id == sess.user_id).first()
    if not user or not user.active:
        return None, None
    return sess, user


def get_current_user(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    sess, user = _load_valid_session(db, token)
    if not user:
        next_path = request.url.path
        if request.url.query:
            next_path += "?" + request.url.query
        response = RedirectResponse(url=f"/login?next={next_path}", status_code=303)
        raise LoginRedirect(response)
    sess.last_seen_at = datetime.utcnow()
    db.commit()
    request.state.user = user
    request.state.csrf_token = generate_csrf_token(sess.session_token)
    return user

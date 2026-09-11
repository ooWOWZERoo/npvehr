"""Login/logout, session-cookie issuance, account management (System
Administrator only), and the authentication audit log."""
from datetime import datetime
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from ehr.models.database import get_db, User, UserSession, AuthAuditEvent
from ehr.auth.security import hash_password, verify_password, new_session_token
from ehr.auth.deps import get_current_user, SESSION_COOKIE_NAME, SESSION_LIFETIME
from ehr.auth.permissions import require_role, USER_MANAGEMENT, AUTH_AUDIT_VIEW, ALL_ROLES, ROLE_LABELS
from ehr.auth.audit import log_auth_event
from ehr.auth import csrf
from ehr.env_info import EHR_ENV

router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS


def _client_ip(request: Request):
    # Best-effort only: this app has no reverse-proxy / X-Forwarded-For trust
    # configuration, so this is simply the ASGI-reported peer address, which
    # can be None depending on how the server is invoked/hosted.
    client = request.client
    return client.host if client else None


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/"):
    # No session exists yet at this point, so login gets its own short-lived
    # double-submit-cookie CSRF token rather than the session-bound scheme
    # used everywhere else (ehr/auth/csrf.py).
    login_csrf_token = csrf.generate_login_csrf()
    response = templates.TemplateResponse(request, "login.html",
        {"next": next, "error": None, "login_csrf_token": login_csrf_token})
    response.set_cookie(csrf.LOGIN_CSRF_COOKIE_NAME, login_csrf_token, httponly=True,
                         secure=request.url.scheme == "https", samesite="lax", max_age=600)
    return response


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, email: str = Form(...), password: str = Form(...),
                  next: str = Form("/"), csrf_token: str = Form(""), db: Session = Depends(get_db)):
    csrf.verify_login_csrf(request.cookies.get(csrf.LOGIN_CSRF_COOKIE_NAME), csrf_token)
    email_norm = email.strip().lower()
    user = db.query(User).filter(User.email == email_norm).first()
    GENERIC_ERROR = "Invalid email or password."
    ok = bool(user and user.active and verify_password(password, user.password_salt, user.password_hash))
    if not ok:
        # Same generic error and same code path whether the email exists or not,
        # so a failed login never reveals which emails have accounts.
        log_auth_event(db, "login_failure", user_id=user.id if user else None,
                        actor_email=email_norm, ip_address=_client_ip(request),
                        detail="invalid credentials" if user else "unknown email")
        db.commit()
        return templates.TemplateResponse(request, "login.html",
            {"next": next, "error": GENERIC_ERROR, "login_csrf_token": csrf_token}, status_code=400)
    token = new_session_token()
    now = datetime.utcnow()
    sess = UserSession(user_id=user.id, session_token=token, created_at=now,
                        last_seen_at=now, expires_at=now + SESSION_LIFETIME, revoked=False)
    db.add(sess)
    user.last_login_at = now
    log_auth_event(db, "login_success", user_id=user.id, ip_address=_client_ip(request))
    db.commit()
    dest = next if (next or "").startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(url=dest, status_code=303)
    is_https = request.url.scheme == "https"
    # Secure cookie flag: set only when this specific request came in over HTTPS.
    # Not hard-required app-wide, since this is routinely run over plain HTTP on
    # localhost for local/dev/demo use -- see build report for this tradeoff.
    response.set_cookie(SESSION_COOKIE_NAME, token, httponly=True, secure=is_https,
                         samesite="lax", max_age=int(SESSION_LIFETIME.total_seconds()))
    return response


@router.post("/logout")
async def logout(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    # No Depends(get_current_user) on this route (logout must still work
    # against an already-expired/invalid session), so request.state.csrf_token
    # isn't set here -- recompute the expected value straight from the raw
    # session cookie instead.
    form = await request.form()
    csrf.verify_or_403(csrf.generate_csrf_token(token) if token else None, form.get("csrf_token"))
    user_id = None
    if token:
        sess = db.query(UserSession).filter(UserSession.session_token == token).first()
        if sess:
            sess.revoked = True
            user_id = sess.user_id
    log_auth_event(db, "logout", user_id=user_id, ip_address=_client_ip(request))
    db.commit()
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


# ---------------------------------------------------------------------------
# Account management -- System Administrator only (spec: cannot be delegated
# to Practice Administrator, which otherwise has full access to everything else).
# ---------------------------------------------------------------------------
@router.get("/admin/users", response_class=HTMLResponse)
def list_users(request: Request, db: Session = Depends(get_db), _user=Depends(get_current_user),
               _role=Depends(require_role(*USER_MANAGEMENT))):
    users = db.query(User).order_by(User.last_name, User.first_name).all()
    return templates.TemplateResponse(request, "admin/users/list.html", {"users": users, "roles": ALL_ROLES})


@router.get("/admin/users/new", response_class=HTMLResponse)
def new_user_form(request: Request, _user=Depends(get_current_user), _role=Depends(require_role(*USER_MANAGEMENT))):
    return templates.TemplateResponse(request, "admin/users/form.html", {"roles": ALL_ROLES, "error": None})


@router.post("/admin/users/new")
def create_user(request: Request, email: str = Form(...), first_name: str = Form(...), last_name: str = Form(...),
                 role: str = Form(...), password: str = Form(...), csrf_token: str = Form(""), db: Session = Depends(get_db),
                 _user=Depends(get_current_user), _role_check=Depends(require_role(*USER_MANAGEMENT))):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    email_norm = email.strip().lower()
    if role not in ALL_ROLES:
        return templates.TemplateResponse(request, "admin/users/form.html",
            {"roles": ALL_ROLES, "error": "Invalid role."}, status_code=400)
    if len(password) < 10:
        # Minimum-length-only policy (spec: complexity enforcement beyond a sane
        # minimum is explicitly out of scope for this pass).
        return templates.TemplateResponse(request, "admin/users/form.html",
            {"roles": ALL_ROLES, "error": "Password must be at least 10 characters."}, status_code=400)
    if db.query(User).filter(User.email == email_norm).first():
        return templates.TemplateResponse(request, "admin/users/form.html",
            {"roles": ALL_ROLES, "error": f"An account already exists for {email_norm}."}, status_code=400)
    salt_hex, hash_hex = hash_password(password)
    new_user = User(email=email_norm, password_hash=hash_hex, password_salt=salt_hex,
                     first_name=first_name, last_name=last_name, role=role, active=True,
                     created_at=datetime.utcnow())
    db.add(new_user); db.flush()
    log_auth_event(db, "account_created", user_id=new_user.id, detail=f"role={role}")
    db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/admin/users/{target_id}/toggle-active")
async def toggle_user_active(request: Request, target_id: int, db: Session = Depends(get_db),
                              _user=Depends(get_current_user), _role_check=Depends(require_role(*USER_MANAGEMENT))):
    form = await request.form()
    csrf.verify_or_403(request.state.csrf_token, form.get("csrf_token"))
    target = db.query(User).filter(User.id == target_id).first()
    if not target:
        return HTMLResponse("Not found", status_code=404)
    target.active = not target.active
    log_auth_event(db, "account_deactivated" if not target.active else "account_activated", user_id=target.id)
    db.commit()
    return RedirectResponse("/admin/users", status_code=303)


# ---------------------------------------------------------------------------
# Authentication audit log -- System Administrator or Read-only/Auditor.
# ---------------------------------------------------------------------------
@router.get("/admin/audit", response_class=HTMLResponse)
def auth_audit_log(request: Request, db: Session = Depends(get_db), _user=Depends(get_current_user),
                    _role=Depends(require_role(*AUTH_AUDIT_VIEW))):
    events = db.query(AuthAuditEvent).order_by(AuthAuditEvent.occurred_at.desc()).limit(300).all()
    users_by_id = {u.id: u for u in db.query(User).all()}
    return templates.TemplateResponse(request, "admin/audit.html", {"events": events, "users_by_id": users_by_id})

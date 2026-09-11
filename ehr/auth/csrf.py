"""CSRF protection (BUILD_BACKLOG.md 7, tracked since v1.x -- spec 15.1,
26.10 item 4, 36.5 item 3, 37.6). Synchronizer-token pattern, session-bound,
built entirely on the stdlib (hmac/secrets) -- no new dependency.

Token derivation is deterministic per session rather than a separate
randomly-generated value stored in the database: `csrf_token =
HMAC-SHA256(SECRET_KEY, session_token)`. This needs no new schema/column --
the session's own already-256-bit-random `session_token` is the per-session
secret being signed, and SECRET_KEY (ehr/env_info.py) is the one new piece
of server-side secret material this introduces.

Delivery: ehr/auth/deps.py's get_current_user sets `request.state.csrf_token`
once, right where it already sets `request.state.user` -- every authenticated
GET renders that value into a <meta> tag in base.html, and static/js/app.js
auto-injects it as a hidden field into every <form> on the page. Verification
happens inline in each POST handler via `verify_or_403`, since each handler
already parses its own form independently (no shared form-parsing layer to
hook into instead).

Login is a special case: no session exists yet when /login is first loaded,
so it gets its own short-lived double-submit-cookie token (generate_login_csrf
/ verify_login_csrf) instead of the session-bound scheme above.
"""
import hmac
import secrets
from fastapi import HTTPException, Request
from ehr.env_info import SECRET_KEY

LOGIN_CSRF_COOKIE_NAME = "login_csrf"


def generate_csrf_token(session_token: str) -> str:
    return hmac.new(SECRET_KEY.encode(), session_token.encode(), "sha256").hexdigest()


def verify_or_403(expected_token, submitted_token) -> None:
    """Pure comparison, raising HTTPException(403) on any mismatch (including
    either side being missing/None). Call near the top of every authenticated
    POST handler, right after parsing the form, e.g.:
        form = await request.form()
        csrf.verify_or_403(request.state.csrf_token, form.get("csrf_token"))
    request.state.csrf_token is set by get_current_user -- for the one route
    that doesn't depend on it (logout, which must work even against an
    already-expired session), pass generate_csrf_token(raw_cookie_value)
    instead."""
    if not expected_token or not submitted_token or not hmac.compare_digest(expected_token, submitted_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")


def generate_login_csrf() -> str:
    """A fresh random token for the one pre-authentication form (login).
    No session exists yet to derive a token from, so this uses the
    double-submit-cookie pattern instead: the same random value is set as a
    cookie and rendered into the form's hidden field; POST /login compares
    the two directly (both come from this server, so equality proves the
    request round-tripped through a real browser holding the login page's
    own cookie -- an attacker's cross-site form cannot supply a cookie value
    it was never given)."""
    return secrets.token_urlsafe(32)


def verify_login_csrf(cookie_value: str, submitted_token: str) -> None:
    if not cookie_value or not submitted_token or not hmac.compare_digest(cookie_value, submitted_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")

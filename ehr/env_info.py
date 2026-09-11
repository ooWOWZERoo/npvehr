"""Deployment-environment helper for the top-bar environment badge, plus the
app's one server-side secret.

EHR_ENV is read from the environment variable of the same name at process
startup. It is a purely INFORMATIONAL label -- it does not gate any
functionality, enable/disable features, or act as a security boundary of
any kind. Operators should set EHR_ENV=production (exact, lowercase) when
running this app against real patient data so staff always see a clear
green "PRODUCTION" pill instead of the amber test/dev warning. Any other
value, or leaving it unset (default: 'development'), renders the amber
"TEST/DEV: <value>" badge so nobody mistakes a sandbox for the real system.

SECRET_KEY is read from the environment variable of the same name. Used
solely to HMAC-sign CSRF tokens (ehr/auth/csrf.py) -- this app has no other
use for a secret (sessions are opaque, DB-validated bearer tokens, not
signed; see ehr/auth/deps.py). If unset, a random key is generated at
process startup as a dev-only convenience: it will not survive a restart,
which is fine for local development (existing sessions/CSRF tokens just get
invalidated) but MUST be set explicitly, and kept stable, for any real
deployment -- same operational expectation as DATABASE_URL.
"""
import os
import secrets

EHR_ENV = os.environ.get("EHR_ENV", "development")
SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_urlsafe(32)
    print("WARNING: SECRET_KEY not set -- generated a random one for this process only. "
          "Set SECRET_KEY in the environment for any deployment that must survive a restart.")

def is_production() -> bool:
    return EHR_ENV == "production"

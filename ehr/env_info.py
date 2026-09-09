"""Deployment-environment helper for the top-bar environment badge.

EHR_ENV is read from the environment variable of the same name at process
startup. It is a purely INFORMATIONAL label -- it does not gate any
functionality, enable/disable features, or act as a security boundary of
any kind. Operators should set EHR_ENV=production (exact, lowercase) when
running this app against real patient data so staff always see a clear
green "PRODUCTION" pill instead of the amber test/dev warning. Any other
value, or leaving it unset (default: 'development'), renders the amber
"TEST/DEV: <value>" badge so nobody mistakes a sandbox for the real system.
"""
import os

EHR_ENV = os.environ.get("EHR_ENV", "development")

def is_production() -> bool:
    return EHR_ENV == "production"

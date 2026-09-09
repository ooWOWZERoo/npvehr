"""AuthAuditEvent logging helper.

SCOPE BOUNDARY (spec: pass 1 of authentication/RBAC): this audit trail covers
authentication and access-control events ONLY -- login_success, login_failure,
logout, session_expired, account_created, account_deactivated,
password_changed, and access_denied. It is intentionally separate from the
existing per-domain audit tables (appointment_audit_events,
appointment_type_audit_events), which log clinical/scheduling DATA changes.
Retrofitting a full "who changed this specific field" audit trail onto every
existing table (patients, eye_exams, prescriptions, etc.) is explicitly OUT
OF SCOPE for this pass -- see the build report for why, and treat this as a
deliberate, documented boundary rather than a gap.
"""
from datetime import datetime
from ehr.models.database import AuthAuditEvent


def log_auth_event(db, event_type, user_id=None, actor_email=None, ip_address=None, detail=None):
    db.add(AuthAuditEvent(event_type=event_type, user_id=user_id, actor_email_attempted=actor_email,
                           ip_address=ip_address, detail=detail, occurred_at=datetime.utcnow()))

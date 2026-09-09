"""Centralized role -> permission mapping.

Rather than scattering ad hoc role checks through every route file, every
route in the app depends on either nothing extra (any authenticated user,
i.e. get_current_user alone) or an extra require_role(...) dependency drawn
from one of the group constants below. Judgment calls made while mapping the
8-role spec matrix onto this app's ACTUAL routes are documented inline next
to each group -- see the build report for the consolidated list.
"""
from fastapi import Depends, HTTPException, Request

SYSTEM_ADMINISTRATOR = "system_administrator"
PRACTICE_ADMINISTRATOR = "practice_administrator"
FRONT_DESK = "front_desk"
TECHNICIAN = "technician"
OPTOMETRIST_PROVIDER = "optometrist_provider"
OPTICIAN = "optician"
BILLING_AND_CLAIMS = "billing_and_claims"
READ_ONLY_AUDITOR = "read_only_auditor"

ALL_ROLES = [
    SYSTEM_ADMINISTRATOR, PRACTICE_ADMINISTRATOR, FRONT_DESK, TECHNICIAN,
    OPTOMETRIST_PROVIDER, OPTICIAN, BILLING_AND_CLAIMS, READ_ONLY_AUDITOR,
]

ROLE_LABELS = {
    SYSTEM_ADMINISTRATOR: "System Administrator",
    PRACTICE_ADMINISTRATOR: "Practice Administrator",
    FRONT_DESK: "Front Desk",
    TECHNICIAN: "Technician",
    OPTOMETRIST_PROVIDER: "Optometrist/Provider",
    OPTICIAN: "Optician",
    BILLING_AND_CLAIMS: "Billing and Claims",
    READ_ONLY_AUDITOR: "Read-only/Auditor",
}

# --- Route-group role sets -------------------------------------------------
# Dashboard + patient/appointment VIEWING: every role gets at least read access
# per the spec matrix (Read-only/Auditor is explicitly global-view; every other
# role's own area always includes at least patient/appointment visibility).
ANY_STAFF = set(ALL_ROLES)

PATIENT_EDIT = {SYSTEM_ADMINISTRATOR, PRACTICE_ADMINISTRATOR, FRONT_DESK, TECHNICIAN, OPTOMETRIST_PROVIDER}
APPOINTMENT_EDIT = set(PATIENT_EDIT)  # same staff who can create/edit patients can create/edit appointments

# JUDGMENT CALL: Front Desk has NO exam access at all per spec (view or edit) --
# unlike Optician/Billing, who get exam VIEW only "for context". Technician
# and Optometrist/Provider get full exam edit; Technician's edit access is
# pretest/data-entry, since there is no exam-signing workflow in this app to
# distinguish "finalized" from "editable" exams either way.
EXAM_VIEW = {SYSTEM_ADMINISTRATOR, PRACTICE_ADMINISTRATOR, TECHNICIAN, OPTOMETRIST_PROVIDER,
             OPTICIAN, BILLING_AND_CLAIMS, READ_ONLY_AUDITOR}
EXAM_EDIT = {SYSTEM_ADMINISTRATOR, PRACTICE_ADMINISTRATOR, TECHNICIAN, OPTOMETRIST_PROVIDER}

# JUDGMENT CALL: Front Desk and Technician get NO prescription access at all --
# prescriptions are not part of either role's listed scope.
RX_VIEW = {SYSTEM_ADMINISTRATOR, PRACTICE_ADMINISTRATOR, OPTOMETRIST_PROVIDER, OPTICIAN,
           BILLING_AND_CLAIMS, READ_ONLY_AUDITOR}
RX_EDIT = {SYSTEM_ADMINISTRATOR, PRACTICE_ADMINISTRATOR, OPTOMETRIST_PROVIDER, OPTICIAN}

ADMIN_SCHEDULING_VIEW = {SYSTEM_ADMINISTRATOR, PRACTICE_ADMINISTRATOR, READ_ONLY_AUDITOR}
ADMIN_SCHEDULING_EDIT = {SYSTEM_ADMINISTRATOR, PRACTICE_ADMINISTRATOR}

STORE_OPS_VIEW = {SYSTEM_ADMINISTRATOR, PRACTICE_ADMINISTRATOR, BILLING_AND_CLAIMS, READ_ONLY_AUDITOR}
STORE_OPS_EDIT = {SYSTEM_ADMINISTRATOR, PRACTICE_ADMINISTRATOR, BILLING_AND_CLAIMS}

# All 5 Claim Management sections are currently GET-only placeholders/views in
# this codebase (no create/edit routes exist yet), so only a VIEW group is needed.
CLAIMS_VIEW = set(STORE_OPS_VIEW)

# JUDGMENT CALL: Catalog and Orders are not explicitly placed in the spec's
# 8-role matrix for every role. Optician is the only clinical role explicitly
# given "Catalog placeholders"/"Orders placeholder" access; System/Practice
# Admin (full-access-except-user-mgmt / full-access roles) and the
# view-everything Read-only/Auditor role are included for completeness.
CATALOG_ORDERS_VIEW = {SYSTEM_ADMINISTRATOR, PRACTICE_ADMINISTRATOR, OPTICIAN, READ_ONLY_AUDITOR}

USER_MANAGEMENT = {SYSTEM_ADMINISTRATOR}
AUTH_AUDIT_VIEW = {SYSTEM_ADMINISTRATOR, READ_ONLY_AUDITOR}


def require_role(*allowed_roles):
    """FastAPI dependency factory returning a dependency that checks
    request.state.user.role (set by get_current_user, which MUST run first --
    every route using this also depends on get_current_user, either directly
    or via the router-level dependency applied in ehr/app.py). Access denials
    are logged to AuthAuditEvent."""
    allowed = set(allowed_roles)

    def _dep(request: Request):
        user = getattr(request.state, "user", None)
        if user is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if user.role not in allowed:
            from ehr.auth.audit import log_auth_event
            from ehr.models.database import SessionLocal
            db = SessionLocal()
            try:
                log_auth_event(db, "access_denied", user_id=user.id,
                                detail=f"{request.method} {request.url.path} (role={user.role})")
                db.commit()
            finally:
                db.close()
            raise HTTPException(status_code=403,
                detail=f"Your role ({ROLE_LABELS.get(user.role, user.role)}) does not have access to this page.")
        return user
    return _dep

"""Record-level authorization (BUILD_BACKLOG.md §37.6 follow-up): before this,
every role check in ehr/auth/permissions.py was role-level only -- any user
with RX_VIEW, say, could see every prescription for every patient in the
practice, with no notion of "my patients" vs. "someone else's patients."

Scope of this round, deliberately narrow: restricts the optometrist_provider
role to only the patients they have a real clinical relationship with (an
Appointment, EyeExam, or Prescription under their own linked Provider
identity, via the new User.provider_id -- see that column's docstring).
Every other role is completely unaffected; this is additive to the existing
role-level checks, never a replacement for them.

Explicitly NOT covered this round (named, not silently skipped): the
appointment calendar/scheduling surfaces (a provider still sees the whole
practice's calendar, not just their own column) and the diagnostic-order/
lab-order lists -- restricting those too is a natural follow-up but a much
larger surface than "a provider's own patient chart," which is the concrete
gap this closes.
"""
from ehr.auth.permissions import OPTOMETRIST_PROVIDER
from ehr.models.database import Appointment, EyeExam, Prescription, Patient


def is_patient_restricted(user) -> bool:
    """True when this user's patient access must be scoped to their own
    patients -- currently just the optometrist_provider role."""
    return bool(user) and user.role == OPTOMETRIST_PROVIDER


def own_patient_ids(db, provider_id) -> set:
    """Every patient this Provider has a real clinical relationship with, via
    any Appointment, EyeExam, or Prescription attributed to them. A provider
    account not linked to a Provider row (provider_id is None) has no defined
    "own patients" set -- returns empty, which callers treat as "sees no
    patients," not "sees every patient" (fail closed, not fail open)."""
    if not provider_id:
        return set()
    ids = set()
    for model in (Appointment, EyeExam, Prescription):
        ids.update(row[0] for row in
                    db.query(model.patient_id).filter(model.provider_id == provider_id).distinct().all())
    return ids


def can_view_patient(db, user, patient_id) -> bool:
    """Whether `user` may view the given patient's record. True for every
    non-restricted role; for a restricted role, true only if the patient is
    in that provider's own_patient_ids()."""
    if not is_patient_restricted(user):
        return True
    return patient_id in own_patient_ids(db, user.provider_id)


def restrict_patient_query(db, user, query):
    """Applies the same restriction to a Patient query (e.g. the patient list
    and quick-switcher search) -- filters to own patients for a restricted
    role, a no-op for every other role."""
    if not is_patient_restricted(user):
        return query
    ids = own_patient_ids(db, user.provider_id)
    return query.filter(Patient.id.in_(ids))

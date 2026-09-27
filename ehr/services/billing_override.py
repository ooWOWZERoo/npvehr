"""NCCI billing-rule enforcement + override (user request: "any exam that
violates billing but is still requested can be overridden by the OD(s) or
General Manager only ... log who overrides, time, date, etc").

Before this, ehr.services.ncci_edits' PTP-edit check was purely advisory
everywhere it appeared (a non-blocking banner on the booking form and the
exam form's return-visit checklist) -- this module is what turns it into an
actual save-time gate for two specific, real CPT-backed test selections:
the New Exam form's return-visit recommendation checklist, and the
appointment booking form's Scheduled Tests. It deliberately does NOT touch
the exam detail page's read-only Billing Preview card (cpt_mapper) -- that's
a post-hoc summary of an already-saved visit, not a test-selection UI a
save can be gated on.
"""
from __future__ import annotations

from ehr.auth.permissions import BILLING_OVERRIDE
from ehr.models.database import BillingOverrideEvent
from ehr.services import ncci_edits


class BillingRuleBlocked(Exception):
    """Raised when an NCCI-flagged test combination can't be saved as-is --
    either the current user's role can't override it at all, or it can but
    hasn't supplied the required override checkbox + reason yet. The
    message is written to be shown directly to the user (same convention as
    every other ValueError-turned-400 in this app's route layer)."""


def enforce(db, user, codes, *, override_checked: bool, override_reason: str,
            source: str, record_id: int):
    """Checks `codes` (CPT codes about to be billed together) against the
    curated NCCI PTP-edit table. Returns normally (no-op) if there's no
    flagged pair. Raises BillingRuleBlocked if there is one and either the
    user's role isn't in BILLING_OVERRIDE, or it is but override_checked/
    override_reason weren't both supplied. On a valid override, stages one
    BillingOverrideEvent per flagged pair onto `db` (the caller still commits)
    and returns normally -- the save proceeds.

    `record_id` should already be a real id (call this after db.flush(),
    not before) so the audit trail actually points at something.

    An indicator-0 pair (modifier_indicator == 0, "no modifier unbundles
    this pair") is never overridable by anyone, regardless of role or the
    override checkbox -- CMS's own edit says no modifier makes this
    combination billable, so an override control here would be encoding an
    incorrect billing shortcut, not a judgment call for a clinician or
    administrator to make. Only indicator-1 (modifier-eligible) pairs go
    through the role/override-checkbox logic below."""
    warnings = ncci_edits.check_codes(codes)
    if not warnings:
        return
    never_overridable = [w for w in warnings if w.modifier_indicator == 0]
    if never_overridable:
        summary = "; ".join(w.message for w in never_overridable)
        raise BillingRuleBlocked(f"{summary} This combination cannot be billed together under any circumstances.")
    summary = "; ".join(w.message for w in warnings)
    if user.role not in BILLING_OVERRIDE:
        raise BillingRuleBlocked(
            f"{summary} Only an Optometrist/Provider or Practice Administrator can override this.")
    if not override_checked or not (override_reason or "").strip():
        raise BillingRuleBlocked(
            f"{summary} Check \"Override Billing Rule\" and give a reason to proceed.")
    for w in warnings:
        db.add(BillingOverrideEvent(user_id=user.id, source=source, record_id=record_id,
            column1=w.column1, column2=w.column2, reason=override_reason.strip()))

"""Rx lab-order lifecycle validation (Real Order Management, replacing the
"/orders/" sidebar placeholder). `RxLabOrder.status` is a plain string, not a
DB enum, matching this app's established convention for lifecycle status
columns (DiagnosticOrder.status, WaitlistEntry.status) -- validation lives
here, in the service/route layer, not the database.

Mirrors ehr.services.diagnostic_orders: `transition()` takes an
already-loaded ORM object and mutates it in place, leaving the caller to
db.commit() -- no session lookups happen here.
"""
from __future__ import annotations

from datetime import datetime

# ---------------------------------------------------------------------------------------- eyeglasses measurements
# PD and seg height are taken when glasses are ORDERED (never at the exam), belong to the order, and are for internal use (the lab): they
# are never shown to the patient. They apply to EYEGLASSES orders only -- a contact lens order records none of them.
LENS_DESIGNS = ("Single Vision", "Bifocal", "Trifocal", "Progressive", "Office-Computer")
SINGLE_VISION = "Single Vision"
MEASURE_FIELDS = ("lens_design", "pd_distance", "pd_od", "pd_os", "pd_near", "seg_height_od", "seg_height_os")
NO_MEASUREMENTS = {k: None for k in MEASURE_FIELDS}
# Typo guards, in mm (a PD of 6.3 for 63 is the classic slip), deliberately wide: they catch a misplaced decimal, not an unusual face.
PD_TOTAL_RANGE, PD_MONO_RANGE, PD_NEAR_RANGE, SEG_RANGE = (40.0, 85.0), (18.0, 45.0), (40.0, 80.0), (5.0, 40.0)


class MeasurementError(ValueError):
    """Glasses measurements failed validation; `.errors` lists every problem so the form can show them all at once."""
    def __init__(self, errors):
        super().__init__(" ".join(errors))
        self.errors = list(errors)


def seg_height_required(lens_design) -> bool:
    """Seg height is required for every lens design except Single Vision."""
    return lens_design in LENS_DESIGNS and lens_design != SINGLE_VISION


def clean_glasses_measurements(lens_design, pd_distance, pd_od, pd_os, pd_near, seg_height_od, seg_height_os) -> dict:
    """Validate and normalise an eyeglasses order's measurements (values may be strings from a form). Rules:
      * lens design must be chosen (it decides whether seg height is needed);
      * PD is ALWAYS required: the distance PD, or BOTH the right and left PD (the total is then their sum); near PD is optional;
      * seg height (right AND left) is required unless the lens is Single Vision; for Single Vision any value entered is discarded.
    Raises MeasurementError listing every problem."""
    errors = []

    def num(label, raw, rng):
        if raw is None or str(raw).strip() == "":
            return None
        try:
            v = float(str(raw).strip())
        except ValueError:
            errors.append(f"{label} must be a number in millimetres.")
            return None
        if not (rng[0] <= v <= rng[1]):
            errors.append(f"{label} of {v:g} mm looks wrong (expected {rng[0]:g}-{rng[1]:g} mm).")
            return None
        return v

    design = (lens_design or "").strip()
    if design not in LENS_DESIGNS:
        errors.append("Choose the lens design.")
        design = None
    total = num("Distance PD", pd_distance, PD_TOTAL_RANGE)
    right, left = num("Right PD", pd_od, PD_MONO_RANGE), num("Left PD", pd_os, PD_MONO_RANGE)
    near = num("Near PD", pd_near, PD_NEAR_RANGE)
    seg_r, seg_l = num("Right seg height", seg_height_od, SEG_RANGE), num("Left seg height", seg_height_os, SEG_RANGE)
    pd_problem = any("PD" in e for e in errors)                # a typo'd PD already has its own message; don't also say it's missing
    if not pd_problem and total is None and (right is None or left is None):
        errors.append("PD is required: enter the distance PD, or both the right and left PD.")
    if total is None and right is not None and left is not None:
        total = round(right + left, 1)
    if seg_height_required(design):
        if (seg_r is None or seg_l is None) and not any("seg height" in e for e in errors):
            errors.append("Seg height is required for this lens design: enter the right and left seg height.")
    else:
        seg_r = seg_l = None                                  # a single vision lens has no segment
    if errors:
        raise MeasurementError(errors)
    return {"lens_design": design, "pd_distance": total, "pd_od": right, "pd_os": left, "pd_near": near,
            "seg_height_od": seg_r, "seg_height_os": seg_l}


PLACED = "placed"
IN_FABRICATION = "in_fabrication"
SHIPPED = "shipped"
RECEIVED = "received"
DISPENSED = "dispensed"
CANCELLED = "cancelled"

# Permissive skip-ahead transitions (a lab may not report every intermediate
# stage), same posture as diagnostic_orders.ALLOWED_TRANSITIONS.
ALLOWED_TRANSITIONS = {
    PLACED: {IN_FABRICATION, SHIPPED, RECEIVED, DISPENSED, CANCELLED},
    IN_FABRICATION: {SHIPPED, RECEIVED, DISPENSED, CANCELLED},
    SHIPPED: {RECEIVED, DISPENSED, CANCELLED},
    RECEIVED: {DISPENSED, CANCELLED},
    DISPENSED: set(),
    CANCELLED: set(),
}


def transition(order, new_status: str, *, dispensed_by_user_id: int = None, cancelled_reason: str = None):
    """Validates `order.status -> new_status` against ALLOWED_TRANSITIONS and
    stamps the fields that go with each destination state. Raises ValueError
    on an invalid transition (including a no-op transition to the same
    state) -- the caller turns that into a 400/flash error, same pattern as
    this app's other user-facing validation."""
    allowed = ALLOWED_TRANSITIONS.get(order.status, set())
    if new_status not in allowed:
        raise ValueError(f"Cannot transition a '{order.status}' lab order to '{new_status}'.")
    order.status = new_status
    if new_status == SHIPPED:
        order.shipped_at = datetime.utcnow()
    elif new_status == RECEIVED:
        order.received_at = datetime.utcnow()
    elif new_status == DISPENSED:
        order.dispensed_at = datetime.utcnow()
        order.dispensed_by_user_id = dispensed_by_user_id
    elif new_status == CANCELLED:
        order.cancelled_at = datetime.utcnow()
        order.cancelled_reason = cancelled_reason

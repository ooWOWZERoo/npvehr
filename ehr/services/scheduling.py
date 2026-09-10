"""Pure(ish) scheduling services: color resolution, duration/buffer calculation,
and provider conflict detection (spec sections 9, 11, 12).

Kept independent of FastAPI/routing so it is unit-testable in isolation.
"""
from datetime import datetime, timedelta

SLOT_UNIT_MINUTES = 5  # spec 11.1: five-minute scheduling resolution


def round_to_slot(minutes: int) -> bool:
    """True if `minutes` is a valid five-minute-aligned duration."""
    return minutes is not None and minutes > 0 and minutes % SLOT_UNIT_MINUTES == 0


def count_countable_tests(appointment_tests) -> int:
    """spec 9.4: only active tests marked counts_toward_color_snapshot, deduplicated
    by diagnostic_test_id, counted."""
    seen = set()
    for t in appointment_tests:
        if t.status != "active" or not t.counts_toward_color_snapshot:
            continue
        if t.diagnostic_test_id in seen:
            continue
        seen.add(t.diagnostic_test_id)
    return len(seen)


def resolve_color(version, relationship: str, is_follow_up: bool, countable_test_count: int):
    """Evaluate a version's ordered color_rules and return (color, reason_code).
    Falls back to the version's base_color if no rule matches (spec 9.5).
    Rule fields with NULL act as wildcards (match any value for that dimension)."""
    rules = sorted(version.color_rules, key=lambda r: r.priority)
    for rule in rules:
        if rule.patient_relationship is not None and rule.patient_relationship != relationship:
            continue
        if rule.is_follow_up is not None and bool(rule.is_follow_up) != bool(is_follow_up):
            continue
        if rule.minimum_countable_tests is not None and countable_test_count < rule.minimum_countable_tests:
            continue
        if rule.maximum_countable_tests is not None and countable_test_count > rule.maximum_countable_tests:
            continue
        return rule.color, (rule.reason_code or f"rule_priority_{rule.priority}")
    return version.base_color, "base_color_default"


def eligible_for_relationship(version, relationship: str) -> bool:
    if relationship == "new":
        return bool(version.allows_new)
    if relationship == "established":
        return bool(version.allows_established)
    return False


def compute_duration_minutes(version, relationship: str) -> int:
    if relationship == "new":
        if not version.allows_new or version.new_duration_minutes is None:
            raise ValueError(f"{version.display_name} is not available to new patients.")
        return version.new_duration_minutes
    if relationship == "established":
        if not version.allows_established or version.established_duration_minutes is None:
            raise ValueError(f"{version.display_name} is not available to established patients.")
        return version.established_duration_minutes
    raise ValueError("patient relationship must be 'new' or 'established'")


def compute_occupied_interval(scheduled_at: datetime, duration_minutes: int,
                               buffer_before_minutes: int = 0, buffer_after_minutes: int = 0):
    """spec 11.3: half-open occupied interval [occupied_start, occupied_end)."""
    occupied_start = scheduled_at - timedelta(minutes=buffer_before_minutes or 0)
    appointment_end = scheduled_at + timedelta(minutes=duration_minutes)
    occupied_end = appointment_end + timedelta(minutes=buffer_after_minutes or 0)
    return occupied_start, appointment_end, occupied_end


def intervals_overlap(start1, end1, start2, end2) -> bool:
    """Half-open interval overlap test: [start1,end1) intersects [start2,end2)?"""
    return start1 < end2 and start2 < end1


ACTIVE_STATUSES = {"scheduled", "checked_in", "in_progress"}  # spec 12.3


def find_provider_conflict(db, provider_id: int, occupied_start: datetime, occupied_end: datetime,
                            exclude_appointment_id: int = None):
    """Provider-only conflict detection (spec 12.1 says schema must support more
    resource classes, but only provider conflicts are enforced in this pass).
    Returns the conflicting Appointment, or None."""
    from ehr.models.database import Appointment  # local import avoids a cycle
    q = db.query(Appointment).filter(
        Appointment.provider_id == provider_id,
        Appointment.status.in_(list(ACTIVE_STATUSES)),
    )
    if exclude_appointment_id is not None:
        q = q.filter(Appointment.id != exclude_appointment_id)
    for other in q.all():
        other_status = other.status.value if hasattr(other.status, "value") else other.status
        if other_status not in ACTIVE_STATUSES:
            continue
        other_start, _end, other_occupied_end = compute_occupied_interval(
            other.scheduled_at, other.duration_minutes or 0,
            other.buffer_before_minutes or 0, other.buffer_after_minutes or 0)
        if intervals_overlap(occupied_start, occupied_end, other_start, other_occupied_end):
            return other
    return None


def compute_resource_interval(scheduled_at: datetime, duration_minutes: int, buffer_before_minutes: int,
                               buffer_after_minutes: int, requirement):
    """A resource requirement can carve out its own sub-interval within the
    appointment (e.g. a device only needed for part of the visit) via its own
    offset_minutes/duration_minutes. If neither is set, the resource occupies
    the SAME full occupied interval (including buffers) already computed for
    the appointment/provider -- reused here, not recomputed independently."""
    if (requirement.offset_minutes or 0) or requirement.duration_minutes is not None:
        start = scheduled_at + timedelta(minutes=requirement.offset_minutes or 0)
        dur = requirement.duration_minutes if requirement.duration_minutes is not None else duration_minutes
        end = start + timedelta(minutes=dur)
        return start, end
    occ_start, _appt_end, occ_end = compute_occupied_interval(
        scheduled_at, duration_minutes, buffer_before_minutes, buffer_after_minutes)
    return occ_start, occ_end


def resolve_resource_for_requirement(db, requirement):
    """Resolve a resource requirement to a concrete Resource. A specific
    resource_id is used directly; a resource_pool_code (no specific resource_id)
    is resolved -- kept intentionally simple per spec -- to the first active
    Resource whose resource_class matches the pool code. Returns None if the
    requirement cannot be resolved to any resource (e.g. an empty pool)."""
    from ehr.models.database import Resource  # local import avoids a cycle
    if requirement.resource_id:
        return db.query(Resource).filter(Resource.id == requirement.resource_id, Resource.active == True).first()
    if requirement.resource_pool_code:
        return (db.query(Resource)
                .filter(Resource.resource_class == requirement.resource_pool_code, Resource.active == True)
                .order_by(Resource.id).first())
    return None


def plan_resource_requirements(db, version, scheduled_at: datetime, duration_minutes: int,
                                buffer_before_minutes: int, buffer_after_minutes: int):
    """Resolve every resource requirement on `version` to (requirement, resource,
    start, end) tuples. Requirements that cannot be resolved to any resource are
    silently skipped (no resource to reserve or conflict-check against)."""
    plan = []
    for requirement in version.resource_requirements:
        resource = resolve_resource_for_requirement(db, requirement)
        if not resource:
            continue
        start, end = compute_resource_interval(
            scheduled_at, duration_minutes, buffer_before_minutes, buffer_after_minutes, requirement)
        plan.append((requirement, resource, start, end))
    return plan


def find_resource_conflict(db, resource_id: int, start: datetime, end: datetime, exclude_appointment_id: int = None):
    """Resource-level conflict detection, mirroring find_provider_conflict:
    same half-open interval math, same ACTIVE_STATUSES status rule (only
    scheduled/checked_in/in_progress appointments consume future resource
    availability). Returns the conflicting AppointmentResourceReservation, or None."""
    from ehr.models.database import AppointmentResourceReservation, Appointment  # local import avoids a cycle
    q = (db.query(AppointmentResourceReservation)
         .join(Appointment, Appointment.id == AppointmentResourceReservation.appointment_id)
         .filter(AppointmentResourceReservation.resource_id == resource_id,
                 AppointmentResourceReservation.active == True,
                 Appointment.status.in_(list(ACTIVE_STATUSES))))
    if exclude_appointment_id is not None:
        q = q.filter(AppointmentResourceReservation.appointment_id != exclude_appointment_id)
    for reservation in q.all():
        if intervals_overlap(start, end, reservation.reserved_start_at, reservation.reserved_end_at):
            return reservation
    return None


def find_resource_blocked_exception(db, resource_id: int, start: datetime, end: datetime):
    """Resource-specific blocked window (AvailabilityException type='blocked'),
    distinct from the practice-wide PracticeClosure table. Returns the matching
    AvailabilityException, or None."""
    from ehr.models.database import AvailabilityException  # local import avoids a cycle
    rows = (db.query(AvailabilityException)
            .filter(AvailabilityException.resource_id == resource_id,
                    AvailabilityException.exception_type == "blocked").all())
    for exception in rows:
        if intervals_overlap(start, end, exception.start_at, exception.end_at):
            return exception
    return None


def find_closure(db, when: datetime):
    """Practice-wide Holidays/Closures check. Returns the matching PracticeClosure
    row (or None) for the calendar date `when` falls on. See PracticeClosure's
    docstring in ehr/models/database.py for why this is a dedicated table rather
    than a resource-scoped AvailabilityException."""
    from ehr.models.database import PracticeClosure  # local import avoids a cycle
    return db.query(PracticeClosure).filter(PracticeClosure.closure_date == when.date().isoformat()).first()


def suggest_relationship(db, patient_id: int, appointment_date_str: str) -> str:
    """spec 10.2: established if a completed EyeExam exists dated before the
    proposed appointment date; otherwise new. exam_date is stored 'YYYY-MM-DD' so
    lexicographic comparison is safe."""
    from ehr.models.database import EyeExam
    exam = (db.query(EyeExam)
            .filter(EyeExam.patient_id == patient_id, EyeExam.exam_date < appointment_date_str)
            .first())
    return "established" if exam else "new"


def find_open_slots(db, provider_id: int, target_date, duration_minutes: int,
                     exclude_appointment_id: int = None):
    """Real open-slot search for one provider on one calendar date. Returns a
    list of available start datetimes where a duration_minutes-long
    appointment fits with no conflict, walked at SLOT_UNIT_MINUTES resolution.

    Building blocks, all reused rather than reimplemented: this provider's
    ProviderAvailabilityTemplate rows for that day-of-week are the base open
    window(s); find_closure() rules out a whole-day practice closure;
    overlapping ProviderAvailabilityException ('blocked') windows and every
    conflicting appointment's occupied interval (via the same
    compute_occupied_interval/intervals_overlap/ACTIVE_STATUSES machinery
    find_provider_conflict already uses) are subtracted from what's left.

    target_date is a datetime.date. Day-of-week alignment: Python's
    date.weekday() (Monday=0..Sunday=6) matches this schema's day_of_week
    convention exactly, so no conversion is needed."""
    from ehr.models.database import ProviderAvailabilityTemplate, ProviderAvailabilityException, Appointment

    day_start = datetime.combine(target_date, datetime.min.time())
    day_end = day_start + timedelta(days=1)

    if find_closure(db, day_start):
        return []

    date_str = target_date.isoformat()
    day_of_week = target_date.weekday()
    rows = (db.query(ProviderAvailabilityTemplate)
            .filter(ProviderAvailabilityTemplate.provider_id == provider_id,
                    ProviderAvailabilityTemplate.day_of_week == day_of_week,
                    ProviderAvailabilityTemplate.active == True)
            .all())
    windows = []
    for row in rows:
        if row.effective_from and date_str < row.effective_from:
            continue
        if row.effective_through and date_str > row.effective_through:
            continue
        start_h, start_m = (int(x) for x in row.start_time.split(":"))
        end_h, end_m = (int(x) for x in row.end_time.split(":"))
        windows.append((day_start.replace(hour=start_h, minute=start_m),
                         day_start.replace(hour=end_h, minute=end_m)))
    if not windows:
        return []

    blockers = []
    exceptions = (db.query(ProviderAvailabilityException)
                  .filter(ProviderAvailabilityException.provider_id == provider_id,
                          ProviderAvailabilityException.exception_type == "blocked")
                  .all())
    for exc in exceptions:
        if intervals_overlap(exc.start_at, exc.end_at, day_start, day_end):
            blockers.append((exc.start_at, exc.end_at))

    appt_q = db.query(Appointment).filter(Appointment.provider_id == provider_id,
                                           Appointment.status.in_(list(ACTIVE_STATUSES)))
    if exclude_appointment_id is not None:
        appt_q = appt_q.filter(Appointment.id != exclude_appointment_id)
    for appt in appt_q.all():
        appt_status = appt.status.value if hasattr(appt.status, "value") else appt.status
        if appt_status not in ACTIVE_STATUSES:
            continue
        occ_start, _end, occ_end = compute_occupied_interval(
            appt.scheduled_at, appt.duration_minutes or 0,
            appt.buffer_before_minutes or 0, appt.buffer_after_minutes or 0)
        if intervals_overlap(occ_start, occ_end, day_start, day_end):
            blockers.append((occ_start, occ_end))

    slots = []
    slot_step = timedelta(minutes=SLOT_UNIT_MINUTES)
    duration_delta = timedelta(minutes=duration_minutes)
    for window_start, window_end in windows:
        cursor = window_start
        while cursor + duration_delta <= window_end:
            candidate_end = cursor + duration_delta
            if not any(intervals_overlap(cursor, candidate_end, b_start, b_end) for b_start, b_end in blockers):
                slots.append(cursor)
            cursor += slot_step
    return slots

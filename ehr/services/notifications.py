"""Automated Confirmations & Reminders (Calendar & Appointments UX Overhaul
Phase 3 -- BUILD_BACKLOG.md 5a).

No real SMS/email vendor is wired up this round (no Twilio/SendGrid account
or API key). send_sms()/send_email() are a swappable interface: today's
implementation only logs what would have been sent and returns a "sent"
outcome, so a later real implementation can be dropped in behind the same
two function signatures without touching any caller.

Every call is gated on the patient's own opt-in for that channel
(Patient.sms_opt_in / email_opt_in, both default False) -- a reminder must
never be sent to a patient who hasn't explicitly turned a channel on, even
though sends are mocked. Every attempt (sent, skipped for no opt-in, or
failed) is recorded in AppointmentReminder for staff visibility and to make
the reminder cron idempotent.
"""
from datetime import datetime
from sqlalchemy.orm import Session

from ehr.models.database import Appointment, AppointmentReminder, WaitlistNotification


def _mock_send(channel: str, recipient: str, message: str) -> bool:
    """Stand-in for a real vendor call (Twilio for SMS, SendGrid for email).
    Always "succeeds" -- there is no real transport to fail against yet."""
    print(f"[mock notification] would send {channel} to {recipient!r}: {message!r}")
    return True


def _already_attempted(db: Session, appointment_id: int, channel: str, kind: str) -> bool:
    """True if any attempt (sent, skipped, or failed) has already been
    recorded for this appointment+channel+kind. Deliberately not scoped to
    'sent' only -- an hourly cron run must not re-log a fresh skip every hour
    for a patient who stays opted out, or duplicate an attempt already made."""
    return (db.query(AppointmentReminder)
            .filter(AppointmentReminder.appointment_id == appointment_id,
                    AppointmentReminder.channel == channel,
                    AppointmentReminder.kind == kind)
            .first() is not None)


def _record(db: Session, appointment: Appointment, channel: str, kind: str,
            status: str, recipient: str, message_body: str) -> AppointmentReminder:
    row = AppointmentReminder(
        appointment_id=appointment.id, channel=channel, kind=kind, status=status,
        recipient=recipient, message_body=message_body,
        scheduled_for=appointment.scheduled_at if kind == "reminder" else None,
        sent_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _compose_message(appointment: Appointment, kind: str) -> str:
    patient = appointment.patient
    provider = appointment.provider
    when = appointment.scheduled_at.strftime("%A, %B %-d at %-I:%M %p")
    verb = "confirmed" if kind == "confirmation" else "coming up"
    return (f"Hi {patient.first_name}, your appointment with Dr. {provider.last_name} "
            f"is {verb} for {when}.")


def send_appointment_notice(db: Session, appointment: Appointment, kind: str) -> list[AppointmentReminder]:
    """Sends (mock) confirmation/reminder notices for one appointment across
    every channel the patient has opted into, skipping (and still recording)
    any channel that isn't opted in. Idempotent per appointment+channel+kind:
    a channel already marked 'sent' for this kind is skipped silently."""
    patient = appointment.patient
    message = _compose_message(appointment, kind)
    results = []

    for channel, opted_in, recipient in (
        ("sms", patient.sms_opt_in, patient.phone),
        ("email", patient.email_opt_in, patient.email),
    ):
        if _already_attempted(db, appointment.id, channel, kind):
            continue
        if not opted_in or not recipient:
            results.append(_record(db, appointment, channel, kind,
                                    "skipped_no_opt_in", recipient or "", message))
            continue
        ok = _mock_send(channel, recipient, message)
        results.append(_record(db, appointment, channel, kind,
                                "sent" if ok else "failed", recipient, message))
    return results


def _waitlist_already_attempted(db: Session, waitlist_entry_id: int, appointment_id: int, channel: str) -> bool:
    """Same not-just-'sent' dedup rule as _already_attempted, keyed on
    (waitlist_entry_id, appointment_id, channel) instead -- a given entry is
    never notified twice about the same freed slot, but can still be
    notified about a *different* one later."""
    return (db.query(WaitlistNotification)
            .filter(WaitlistNotification.waitlist_entry_id == waitlist_entry_id,
                    WaitlistNotification.appointment_id == appointment_id,
                    WaitlistNotification.channel == channel)
            .first() is not None)


def _record_waitlist(db: Session, waitlist_entry_id: int, appointment_id: int, channel: str,
                      status: str, recipient: str, message_body: str) -> WaitlistNotification:
    row = WaitlistNotification(waitlist_entry_id=waitlist_entry_id, appointment_id=appointment_id,
        channel=channel, status=status, recipient=recipient, message_body=message_body,
        sent_at=datetime.utcnow())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def send_waitlist_opening_notices(db: Session, freed_appointment: Appointment) -> list[WaitlistNotification]:
    """Waitlist auto-notify (BUILD_BACKLOG.md 5a, the deferred follow-up from
    Phase 2/3): called right when an appointment transitions to cancelled.
    Finds every active WaitlistEntry that matches the slot this appointment
    just freed (ehr.services.scheduling.find_matching_waitlist_entries -- the
    exact same matching logic that already surfaces these entries to staff
    on the appointment detail page) and sends each one a (mock) notice per
    opted-in channel, same gating and same mock-only posture as
    send_appointment_notice above. Idempotent per (waitlist_entry, freed
    appointment, channel)."""
    from ehr.services import scheduling as sched  # local import avoids a cycle (scheduling doesn't import this module)

    matches = sched.find_matching_waitlist_entries(
        db, freed_appointment.provider_id, freed_appointment.appointment_type_version_id,
        freed_appointment.scheduled_at.date())
    provider = freed_appointment.provider
    when = freed_appointment.scheduled_at.strftime("%A, %B %-d at %-I:%M %p")
    results = []

    for entry in matches:
        patient = entry.patient
        message = (f"Hi {patient.first_name}, a slot just opened up with Dr. {provider.last_name} "
                    f"on {when}. Call the office if you'd like to grab it.")
        for channel, opted_in, recipient in (
            ("sms", patient.sms_opt_in, patient.phone),
            ("email", patient.email_opt_in, patient.email),
        ):
            if _waitlist_already_attempted(db, entry.id, freed_appointment.id, channel):
                continue
            if not opted_in or not recipient:
                results.append(_record_waitlist(db, entry.id, freed_appointment.id, channel,
                                                  "skipped_no_opt_in", recipient or "", message))
                continue
            ok = _mock_send(channel, recipient, message)
            results.append(_record_waitlist(db, entry.id, freed_appointment.id, channel,
                                              "sent" if ok else "failed", recipient, message))
    return results


def send_portal_login_link(patient, link_url: str) -> None:
    """Patient self-service portal magic link (Phase 4 -- BUILD_BACKLOG.md
    5a). Not gated on Patient.email_opt_in -- opt-in guards unsolicited
    reminder/marketing-style contact, not a login link the patient just
    explicitly requested by typing their own email into the login form.
    Same mock-only posture as the rest of this module: no real email vendor
    is wired up, so this only logs what would have been sent."""
    if not patient.email:
        return
    _mock_send("email", patient.email, f"Your New Path Vision sign-in link: {link_url}")

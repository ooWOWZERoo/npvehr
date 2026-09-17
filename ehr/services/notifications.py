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

from ehr.models.database import Appointment, AppointmentReminder


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

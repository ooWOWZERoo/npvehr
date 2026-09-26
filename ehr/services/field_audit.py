"""Per-record "who changed this field, and to what value" audit trail (spec
§37.1/§37.6). One generic FieldChangeAuditEvent table, keyed by
(table_name, record_id) -- see that model's docstring in
ehr/models/database.py for the full scoping rationale (why one generic
table, why only Patient/Provider this round, why AppointmentAuditEvent/
AppointmentTypeAuditEvent are left as their own separate thing).
"""
from ehr.models.database import FieldChangeAuditEvent


def record_field_changes(db, table_name: str, record_id: int, before: dict, after: dict, user_id: int):
    """Compares `before` and `after` (field_name -> value dicts, same keys)
    and inserts one FieldChangeAuditEvent row per field whose value actually
    changed. Values are stored as plain strings (str(value) or None for a
    None value) -- a legible change log for staff to read, not a typed diff,
    matching AppointmentAuditEvent's own old_value/new_value string columns.
    Call this with `before` snapshotted BEFORE mutating the record and
    `after` read AFTER, both restricted to the fields the caller wants
    tracked (not necessarily every column on the model)."""
    for field_name, new_value in after.items():
        old_value = before.get(field_name)
        if old_value == new_value:
            continue
        db.add(FieldChangeAuditEvent(
            table_name=table_name, record_id=record_id, field_name=field_name,
            old_value=None if old_value is None else str(old_value),
            new_value=None if new_value is None else str(new_value),
            changed_by_user_id=user_id,
        ))

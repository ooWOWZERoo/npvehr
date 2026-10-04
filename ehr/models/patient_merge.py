"""Record of a duplicate-patient merge (append-only table; no existing table changed).

A merge moves every record linked to the duplicate onto the patient being kept and then removes the now-empty duplicate row.
The event stores what is needed to put it back: a snapshot of the duplicate's own fields, the kept patient's fields as they
were before any were changed, the ids of every row that was moved (by table), and any rows that had to be dropped because
the kept patient already had an equivalent (e.g. a second primary-care link). `details_json` holds all of that, so it is
patient data of the same sensitivity as the chart; the app is for test or de-identified data only.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ehr.models.database import Base


class PatientMergeEvent(Base):
    __tablename__ = "patient_merge_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    survivor_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"))
    merged_patient_id: Mapped[int] = mapped_column()                  # the removed duplicate's old id (its row no longer exists)
    merged_label: Mapped[str] = mapped_column(String(200))            # "Last, First" for the history list
    status: Mapped[str] = mapped_column(String(8), default="merged")  # merged | undone
    reason: Mapped[str] = mapped_column(String(500))
    moved_total: Mapped[int] = mapped_column(default=0)
    dropped_total: Mapped[int] = mapped_column(default=0)
    details_json: Mapped[Optional[str]] = mapped_column(Text)
    merged_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    merged_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    undone_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    undone_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    __table_args__ = (CheckConstraint("status IN ('merged','undone')", name="ck_patient_merge_status"),
                      Index("ix_patient_merge_survivor", "survivor_id"))

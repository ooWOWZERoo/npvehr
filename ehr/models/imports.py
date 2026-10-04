"""Data-import bookkeeping and the patient recall table (recall-report import).

Append-only tables, no existing table changed:

  * data_import_batches        -- one row per uploaded file: who, when, the test-data attestation and its
                                  reason, and the outcome counts. The raw file is held in `file_bytes` ONLY while
                                  the batch is staged (preview), and is cleared as soon as the import is applied,
                                  discarded, or the batch ages out -- the PHI-bearing upload is not kept.
  * data_import_batch_patients -- which patients a batch CREATED, so a batch can be undone without touching
                                  patients that already existed or that have since gained clinical records.
  * patient_recalls            -- a patient's due-back dates by recall type (the Recalls tab), including the
                                  report's last-exam and next-appointment snapshot. Informational: nothing sends
                                  reminders from it.
"""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (Boolean, Date, DateTime, ForeignKey, Index, LargeBinary, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from ehr.models.database import Base


class DataImportBatch(Base):
    __tablename__ = "data_import_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), default="recall_report")
    file_name: Mapped[Optional[str]] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(12), default="staged")    # staged | imported | undone | failed
    file_bytes: Mapped[Optional[bytes]] = mapped_column(LargeBinary)      # cleared once applied / discarded / aged out
    uploaded_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    imported_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    attestation_reason: Mapped[Optional[str]] = mapped_column(String(500))   # "test / de-identified data" confirmation + why
    rows_total: Mapped[int] = mapped_column(default=0)
    rows_skipped: Mapped[int] = mapped_column(default=0)
    patients_created: Mapped[int] = mapped_column(default=0)
    patients_matched: Mapped[int] = mapped_column(default=0)
    recalls_created: Mapped[int] = mapped_column(default=0)
    patients_undone: Mapped[int] = mapped_column(default=0)
    patients_kept_on_undo: Mapped[int] = mapped_column(default=0)
    summary_text: Mapped[Optional[str]] = mapped_column(Text)               # warnings / error text (no patient identifiers)
    __table_args__ = (Index("ix_data_import_batches_status", "status", "created_at"),)


class DataImportBatchPatient(Base):
    __tablename__ = "data_import_batch_patients"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("data_import_batches.id", ondelete="CASCADE"))
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"))
    __table_args__ = (UniqueConstraint("batch_id", "patient_id", name="uq_import_batch_patient"),
                      Index("ix_import_batch_patients_patient", "patient_id"))


class PatientRecall(Base):
    __tablename__ = "patient_recalls"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"))
    recall_type: Mapped[str] = mapped_column(String(80))                   # as labelled in the source, e.g. "12 Month Adult"
    interval_months: Mapped[Optional[int]] = mapped_column()                # parsed from the label when it states one
    audience: Mapped[Optional[str]] = mapped_column(String(10))             # 'adult' | 'child' | None
    due_date: Mapped[date] = mapped_column(Date)
    last_exam_date: Mapped[Optional[date]] = mapped_column(Date)
    never_examined: Mapped[bool] = mapped_column(Boolean, default=False)    # the report said "Never"
    next_appt_date: Mapped[Optional[date]] = mapped_column(Date)
    import_batch_id: Mapped[Optional[int]] = mapped_column(ForeignKey("data_import_batches.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("patient_id", "recall_type", "due_date", name="uq_patient_recall"),
                      Index("ix_patient_recalls_due", "due_date"))


class DataImportBatchRecord(Base):
    """Rows a non-patient import batch CREATED (e.g. ROS prompts/rules), by table name and id, so the batch can be
    undone without touching anything that existed before or that has since been reviewed or used."""
    __tablename__ = "data_import_batch_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("data_import_batches.id", ondelete="CASCADE"))
    record_table: Mapped[str] = mapped_column(String(60))
    record_id: Mapped[int] = mapped_column()
    __table_args__ = (Index("ix_import_batch_records_batch", "batch_id", "record_table"),)

"""Clinical safety flags and warning rules (ROS plan stage 5) -- append-only tables, no existing
table changed. Same posture as the ROS catalog (ehr/models/ros.py): the practice's clinicians
define the rules; nothing here ships clinical content of its own beyond the neutral patient-status
questions seeded by migration 056, and a rule is SILENT until a clinician signs it off with a
source citation.

  * safety_flag_types       -- the patient-status questions ("Pregnant", "Takes a blood thinner", ...)
  * patient_safety_flags    -- a patient's current answer per question (yes/no; no row = unknown),
                               with who recorded it and when; changes are written to the field audit log
  * safety_rules            -- "when <flag> is yes (and, optionally, the exam text mentions <keyword>),
                               warn with <text>"; fires only when active AND reviewed
  * exam_safety_acknowledgements -- the clinician's acknowledgement of a fired rule at sign time, with
                               a snapshot of the warning text as it read then

Decision support for staff only: a warning never changes the note, billing, or an order.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import (Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ehr.models.database import Base


class SafetyFlagType(Base):
    __tablename__ = "safety_flag_types"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    label: Mapped[str] = mapped_column(String(120))
    description: Mapped[Optional[str]] = mapped_column(String(255))
    sort_order: Mapped[int] = mapped_column(default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class PatientSafetyFlag(Base):
    __tablename__ = "patient_safety_flags"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"))
    flag_type_id: Mapped[int] = mapped_column(ForeignKey("safety_flag_types.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(3))                     # 'yes' | 'no'
    note: Mapped[Optional[str]] = mapped_column(String(255))
    recorded_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    flag_type: Mapped["SafetyFlagType"] = relationship()
    __table_args__ = (UniqueConstraint("patient_id", "flag_type_id", name="uq_patient_safety_flag"),
                      CheckConstraint("status IN ('yes','no')", name="ck_patient_safety_flag_status"))


class SafetyRule(Base):
    __tablename__ = "safety_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    flag_type_id: Mapped[int] = mapped_column(ForeignKey("safety_flag_types.id", ondelete="RESTRICT"))
    warning_text: Mapped[str] = mapped_column(String(500))
    keyword: Mapped[Optional[str]] = mapped_column(String(80))          # blank = fires whenever the flag is yes
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    source_citation: Mapped[Optional[str]] = mapped_column(String(255))
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewed_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    flag_type: Mapped["SafetyFlagType"] = relationship()
    __table_args__ = (Index("ix_safety_rules_flag_active", "flag_type_id", "is_active", "reviewed"),)


class ExamSafetyAcknowledgement(Base):
    __tablename__ = "exam_safety_acknowledgements"
    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("eye_exams.id", ondelete="CASCADE"))
    rule_id: Mapped[int] = mapped_column(ForeignKey("safety_rules.id", ondelete="RESTRICT"))
    warning_text_snapshot: Mapped[str] = mapped_column(String(500))
    acknowledged_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    acknowledged_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    rule: Mapped["SafetyRule"] = relationship()
    __table_args__ = (UniqueConstraint("exam_id", "rule_id", name="uq_exam_safety_ack"),)

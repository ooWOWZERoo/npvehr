"""Patient medication and allergy lists, and the practice's curated drug classes (append-only tables, no existing
table changed; `patients.allergies` free text stays as it was).

  * patient_medications      -- what a patient takes or took (never deleted; stopping sets status + stop date)
  * patient_allergies        -- allergen, reaction, severity, status
  * medication_list_reviews  -- each time a clinician confirms a list ("no changes", "updated", or "patient reports
                                none"), so an EMPTY list can mean "reviewed: none" instead of "unknown"
  * medication_classes       -- practice-defined drug classes the safety rules care about (seeded empty)
  * medication_class_terms   -- generic/brand names in a class; a term classifies nothing until a reviewer signs it
                                off with a source citation (same sign-off shape as the ROS and safety rules)
  * safety_flag_class_links  -- links a patient safety flag to classes, so the flag can be derived from the list

Decision support for staff only; nothing here is transmitted anywhere and no drug database is bundled.
Status values are plain strings with CHECK constraints (no DB ENUMs).
"""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ehr.models.database import Base


class PatientMedication(Base):
    __tablename__ = "patient_medications"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(160))                       # as entered, e.g. "Latanoprost 0.005%"
    strength: Mapped[Optional[str]] = mapped_column(String(80))
    route: Mapped[Optional[str]] = mapped_column(String(40))
    frequency: Mapped[Optional[str]] = mapped_column(String(80))
    eye: Mapped[Optional[str]] = mapped_column(String(2))                # OD / OS / OU for ocular medications
    indication: Mapped[Optional[str]] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(8), default="active")     # active | stopped
    start_date: Mapped[Optional[date]] = mapped_column(Date)
    stop_date: Mapped[Optional[date]] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(20), default="staff")     # staff | patient | imported
    note: Mapped[Optional[str]] = mapped_column(String(255))
    recorded_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (CheckConstraint("status IN ('active','stopped')", name="ck_patient_medication_status"),
                      Index("ix_patient_medications_patient", "patient_id", "status"))


class PatientAllergy(Base):
    __tablename__ = "patient_allergies"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"))
    allergen: Mapped[str] = mapped_column(String(160))
    reaction: Mapped[Optional[str]] = mapped_column(String(160))
    severity: Mapped[Optional[str]] = mapped_column(String(10))          # mild | moderate | severe
    status: Mapped[str] = mapped_column(String(8), default="active")     # active | inactive
    note: Mapped[Optional[str]] = mapped_column(String(255))
    recorded_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (CheckConstraint("status IN ('active','inactive')", name="ck_patient_allergy_status"),
                      CheckConstraint("severity IS NULL OR severity IN ('mild','moderate','severe')", name="ck_patient_allergy_severity"),
                      Index("ix_patient_allergies_patient", "patient_id", "status"))


class MedicationListReview(Base):
    __tablename__ = "medication_list_reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(12))                        # medications | allergies
    outcome: Mapped[str] = mapped_column(String(14))                     # no_changes | updated | none_reported
    exam_id: Mapped[Optional[int]] = mapped_column(ForeignKey("eye_exams.id", ondelete="SET NULL"))
    item_count: Mapped[int] = mapped_column(default=0)                   # active items at the time
    reviewed_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (CheckConstraint("kind IN ('medications','allergies')", name="ck_med_review_kind"),
                      CheckConstraint("outcome IN ('no_changes','updated','none_reported')", name="ck_med_review_outcome"),
                      Index("ix_med_reviews_patient", "patient_id", "kind", "reviewed_at"))


class MedicationClass(Base):
    __tablename__ = "medication_classes"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    label: Mapped[str] = mapped_column(String(120))
    description: Mapped[Optional[str]] = mapped_column(String(255))
    sort_order: Mapped[int] = mapped_column(default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    terms: Mapped[list["MedicationClassTerm"]] = relationship(back_populates="med_class", cascade="all, delete-orphan",
                                                              passive_deletes=True, order_by="MedicationClassTerm.term")


class MedicationClassTerm(Base):
    __tablename__ = "medication_class_terms"
    id: Mapped[int] = mapped_column(primary_key=True)
    class_id: Mapped[int] = mapped_column(ForeignKey("medication_classes.id", ondelete="CASCADE"))
    term: Mapped[str] = mapped_column(String(80))                        # lower-case generic or brand name
    source_citation: Mapped[Optional[str]] = mapped_column(String(255))
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewed_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    med_class: Mapped["MedicationClass"] = relationship(back_populates="terms")
    __table_args__ = (UniqueConstraint("class_id", "term", name="uq_med_class_term"),)


class SafetyFlagClassLink(Base):
    __tablename__ = "safety_flag_class_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    flag_type_id: Mapped[int] = mapped_column(ForeignKey("safety_flag_types.id", ondelete="CASCADE"))
    class_id: Mapped[int] = mapped_column(ForeignKey("medication_classes.id", ondelete="CASCADE"))
    __table_args__ = (UniqueConstraint("flag_type_id", "class_id", name="uq_flag_class_link"),)

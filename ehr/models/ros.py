"""Review-of-Systems clinical decision-support catalog (append-only plugin
tables). Three NEW tables, no change to any existing table:

  * ros_master             -- the catalog of ROS prompts per body system
  * ros_icd10_test_mapping -- curated "this ROS finding -> suggest this ICD-10
                              and this test (CPT)" rules, each carrying a
                              source citation and a compliance-review sign-off
  * encounter_ros_responses-- per-exam granular answers (created now, written
                              by a later phase; empty today)

Same posture as ap_composer / cpt_mapper / ncci_edits: a narrow curated lookup
table sourced from a primary document, staff-facing decision support only --
nothing here is ever transmitted or submitted. A mapping can only be used to
raise a compliance advisory once `reviewed` is true, which requires a citation
(see ehr/routes/admin_ros.py); until then it is suggestion-only.

Uses SQLAlchemy 2.x Mapped/mapped_column on the app's own Base so create_all()
builds these tables for free (no hand-written CREATE TABLE) and the FKs resolve
against the real eye_exams table. Status/review values are plain strings with
CHECK constraints, not DB ENUMs (Postgres ENUMs are separate DB objects).
"""
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ehr.models.database import Base

ROS_SYSTEMS = [
    "Constitutional", "Ocular", "Cardiovascular", "Respiratory", "Gastrointestinal",
    "Genitourinary", "Musculoskeletal", "Integumentary", "Neurological", "Psychiatric",
    "Endocrine", "Hematologic / Lymphatic", "Allergic / Immunologic",
]


class RosMaster(Base):
    __tablename__ = "ros_master"
    id: Mapped[int] = mapped_column(primary_key=True)
    body_system: Mapped[str] = mapped_column(String(50))
    prompt_text: Mapped[str] = mapped_column(String(255))
    sort_order: Mapped[int] = mapped_column(default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    mappings: Mapped[List["RosIcd10TestMapping"]] = relationship(
        back_populates="ros_item", cascade="all, delete-orphan", passive_deletes=True)
    __table_args__ = (UniqueConstraint("body_system", "prompt_text", name="uq_ros_master_system_prompt"),
                      Index("ix_ros_master_active_sort", "is_active", "sort_order"))


class RosIcd10TestMapping(Base):
    __tablename__ = "ros_icd10_test_mapping"
    id: Mapped[int] = mapped_column(primary_key=True)
    ros_item_id: Mapped[int] = mapped_column(ForeignKey("ros_master.id", ondelete="CASCADE"))
    suggested_icd10: Mapped[str] = mapped_column(String(10))           # e.g. E11.9
    icd10_pattern: Mapped[Optional[str]] = mapped_column(String(10))   # supporting family, e.g. E11 (blank = exact suggested)
    recommended_cpt: Mapped[str] = mapped_column(String(10))
    compliance_rule: Mapped[Optional[str]] = mapped_column(Text)
    source_citation: Mapped[Optional[str]] = mapped_column(String(255))  # LCD / policy the rule comes from
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False)       # compliance sign-off
    reviewed_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    ros_item: Mapped["RosMaster"] = relationship(back_populates="mappings")
    __table_args__ = (UniqueConstraint("ros_item_id", "suggested_icd10", "recommended_cpt", name="uq_ros_map_triplet"),
                      Index("ix_ros_map_cpt", "recommended_cpt"))


class EncounterRosResponse(Base):
    __tablename__ = "encounter_ros_responses"
    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("eye_exams.id", ondelete="CASCADE"))  # this app's "encounter"
    ros_item_id: Mapped[int] = mapped_column(ForeignKey("ros_master.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(10), default="negative")
    notes: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    ros_item: Mapped["RosMaster"] = relationship()
    __table_args__ = (UniqueConstraint("exam_id", "ros_item_id", name="uq_ros_resp_exam_item"),
                      CheckConstraint("status IN ('positive','negative')", name="ck_ros_resp_status"))

"""Care-coordination tables for the diabetic-retinopathy PCP letter (append-only; no existing table changed):

  * outside_practitioners    -- one global directory of outside clinicians (PCPs). Fax is stored as digits only; an
                                optional NPI and Direct-messaging address are kept for later routing.
  * patient_primary_care     -- which directory entry is a patient's primary-care provider (a link table, so the
                                patients table stays untouched; one per patient)
  * communication_templates  -- the practice-editable letter body (placeholders in {braces}); seeded by migration 057
  * pcp_communications       -- a letter to the managing physician (snapshot of the text as written, severity and
                                macular-edema statement as discrete fields, who/when/how it was SENT), or a documented
                                exclusion (patient refusal / medical contraindication)

The app never transmits anything: "sent" is a staff attestation of how the letter left the office. Decision support for
staff only; see ehr/services/pcp_letter.py for the trigger rule and PCP_LETTER notes in the spec (§116).
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ehr.models.database import Base


class OutsidePractitioner(Base):
    __tablename__ = "outside_practitioners"
    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80))
    credentials: Mapped[Optional[str]] = mapped_column(String(40))        # MD, DO, NP ...
    practice_name: Mapped[Optional[str]] = mapped_column(String(160))
    npi: Mapped[Optional[str]] = mapped_column(String(10))
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    fax: Mapped[Optional[str]] = mapped_column(String(10))                # 10 digits, unformatted
    direct_address: Mapped[Optional[str]] = mapped_column(String(160))
    street_line_1: Mapped[Optional[str]] = mapped_column(String(120))
    street_line_2: Mapped[Optional[str]] = mapped_column(String(120))
    city: Mapped[Optional[str]] = mapped_column(String(80))
    state: Mapped[Optional[str]] = mapped_column(String(2))
    postal_code: Mapped[Optional[str]] = mapped_column(String(10))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (Index("ix_outside_practitioners_name", "last_name", "first_name"),)


class PatientPrimaryCare(Base):
    __tablename__ = "patient_primary_care"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"), unique=True)
    practitioner_id: Mapped[int] = mapped_column(ForeignKey("outside_practitioners.id", ondelete="RESTRICT"))
    set_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    set_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    practitioner: Mapped["OutsidePractitioner"] = relationship()


class CommunicationTemplate(Base):
    __tablename__ = "communication_templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    title: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(Text)
    updated_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PcpCommunication(Base):
    __tablename__ = "pcp_communications"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id", ondelete="CASCADE"))
    exam_id: Mapped[int] = mapped_column(ForeignKey("eye_exams.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(10), default="drafted")      # drafted | sent | excluded
    practitioner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("outside_practitioners.id", ondelete="SET NULL"))
    recipient_snapshot: Mapped[Optional[str]] = mapped_column(String(400))   # name / practice / fax as they were
    severity: Mapped[Optional[str]] = mapped_column(String(20))              # mild | moderate | severe | proliferative
    macular_edema: Mapped[Optional[str]] = mapped_column(String(8))          # present | absent
    dilated_exam_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    body_snapshot: Mapped[Optional[str]] = mapped_column(Text)
    sent_method: Mapped[Optional[str]] = mapped_column(String(12))           # fax | mail | direct | phone | handed
    sent_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    exclusion_reason: Mapped[Optional[str]] = mapped_column(String(24))      # patient_refusal | medical_contraindication | other
    exclusion_note: Mapped[Optional[str]] = mapped_column(String(255))
    created_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    practitioner: Mapped[Optional["OutsidePractitioner"]] = relationship()
    __table_args__ = (
        CheckConstraint("status IN ('drafted','sent','excluded')", name="ck_pcp_comm_status"),
        CheckConstraint("severity IS NULL OR severity IN ('mild','moderate','severe','proliferative')", name="ck_pcp_comm_severity"),
        CheckConstraint("macular_edema IS NULL OR macular_edema IN ('present','absent')", name="ck_pcp_comm_edema"),
        Index("ix_pcp_comm_patient", "patient_id", "status", "created_at"),
        Index("ix_pcp_comm_exam", "exam_id"),
    )

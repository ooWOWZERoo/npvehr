import enum
import os
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Float, Text, DateTime, Boolean, ForeignKey, Enum, Index, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

# DATABASE_URL is read from the environment so the same code runs against local
# SQLite (default, for local dev -- unusable on Vercel, whose filesystem is
# read-only/ephemeral outside /tmp) and a real Postgres instance (e.g. Neon) in
# deployed environments.
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./ehr.db")
# Neon (and most providers) hand out plain "postgresql://..." strings, which
# SQLAlchemy defaults to the psycopg2 driver. This app depends on psycopg
# (v3) instead, so rewrite the scheme to select it explicitly rather than
# also carrying a psycopg2 dependency just for URL compatibility.
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Base(DeclarativeBase):
    pass

class AppointmentStatus(str, enum.Enum):
    scheduled = "scheduled"
    checked_in = "checked_in"
    in_progress = "in_progress"
    completed = "completed"
    cancelled = "cancelled"
    no_show = "no_show"

class Patient(Base):
    __tablename__ = "patients"
    id = Column(Integer, primary_key=True, index=True)
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    # Optional preferred/"goes by" name, shown alongside the legal name wherever the
    # patient's full name is displayed prominently, e.g. Shaw, John "Johnny".
    preferred_name = Column(String)
    # Medical record number: free-text, manually entered. There is no auto-numbering
    # system behind this yet, so nothing generates a value here -- it is otherwise the
    # same "honest placeholder field" pattern as balance_due below. Uniqueness IS
    # enforced (migration 010): a partial UNIQUE index on (mrn) WHERE mrn IS NOT NULL,
    # so blank/unassigned MRNs never collide with each other but two patients can no
    # longer share a real value. Routes check for a conflict before saving and return
    # a friendly error rather than letting the constraint raise a raw DB exception.
    mrn = Column(String)
    date_of_birth = Column(String)
    gender = Column(String)
    phone = Column(String)
    email = Column(String)
    address = Column(String)
    city = Column(String)
    state = Column(String)
    zip_code = Column(String)
    insurance_provider = Column(String)
    insurance_id = Column(String)
    emergency_contact_name = Column(String)
    emergency_contact_phone = Column(String)
    allergies = Column(Text)
    medical_history = Column(Text)
    ocular_history = Column(Text)
    family_ocular_history = Column(Text)
    photo_path = Column(String)
    # Manually-entered balance snapshot: no billing/ledger module exists yet, so this is a
    # real but hand-maintained number (positive = patient owes money, negative = credit/
    # overpayment on file, 0/None = even) rather than anything computed from transactions.
    balance_due = Column(Float)
    created_at = Column(DateTime, default=datetime.utcnow)
    appointments = relationship("Appointment", back_populates="patient", cascade="all, delete-orphan")
    eye_exams = relationship("EyeExam", back_populates="patient", cascade="all, delete-orphan")
    prescriptions = relationship("Prescription", back_populates="patient", cascade="all, delete-orphan")

class Provider(Base):
    __tablename__ = "providers"
    id = Column(Integer, primary_key=True, index=True)
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    license_number = Column(String)
    npi = Column(String)
    specialty = Column(String, default="Optometry")
    appointments = relationship("Appointment", back_populates="provider")
    eye_exams = relationship("EyeExam", back_populates="provider")
    prescriptions = relationship("Prescription", back_populates="provider")

class Appointment(Base):
    __tablename__ = "appointments"
    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)
    provider_id = Column(Integer, ForeignKey("providers.id"), nullable=False)
    scheduled_at = Column(DateTime, nullable=False)
    duration_minutes = Column(Integer, default=30)
    reason = Column(String)
    status = Column(Enum(AppointmentStatus), default=AppointmentStatus.scheduled)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    # --- Appointment Scheduling Module additions (spec section 18.2) ---
    # These columns are added to the existing sqlite table by the migration
    # runner in ehr/db/migrations.py (idempotent ALTER TABLE), not by create_all().
    appointment_type_version_id = Column(Integer, ForeignKey("appointment_type_versions.id"))
    patient_relationship_at_booking = Column(String, default="established")  # 'new' | 'established'
    patient_relationship_source = Column(String, default="automatic")  # 'automatic' | 'manual_override'
    patient_relationship_override_reason = Column(Text)
    is_follow_up = Column(Boolean, default=False)
    scheduled_end_at = Column(DateTime)
    buffer_before_minutes = Column(Integer, default=0)
    buffer_after_minutes = Column(Integer, default=0)
    arrival_lead_minutes = Column(Integer, default=0)
    resolved_color = Column(String)
    resolved_color_reason = Column(String)
    duration_overridden = Column(Boolean, default=False)
    duration_override_reason = Column(Text)
    conflict_overridden = Column(Boolean, default=False)
    conflict_override_reason = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # Attribution (spec 18.2's deferral resolved now that a User/auth model exists,
    # v2.4): set explicitly by the route on create/edit/reschedule, not by an ORM
    # default -- there's no reliable way to get "the current request's user" from
    # inside a Column default. Nullable: legacy/pre-migration appointments have no
    # attributable user, and both stay unset if a mutation never happens through
    # one of those routes.
    created_by_user_id = Column(Integer, ForeignKey("users.id"))
    updated_by_user_id = Column(Integer, ForeignKey("users.id"))

    patient = relationship("Patient", back_populates="appointments")
    provider = relationship("Provider", back_populates="appointments")
    appointment_type_version = relationship("AppointmentTypeVersion")
    created_by = relationship("User", foreign_keys=[created_by_user_id])
    updated_by = relationship("User", foreign_keys=[updated_by_user_id])
    tests = relationship("AppointmentTest", back_populates="appointment", cascade="all, delete-orphan")
    resource_reservations = relationship("AppointmentResourceReservation", back_populates="appointment", cascade="all, delete-orphan")
    audit_events = relationship("AppointmentAuditEvent", back_populates="appointment", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_appointments_scheduled_end_at", "scheduled_end_at"),
        Index("ix_appointments_provider_scheduled", "provider_id", "scheduled_at"),
        Index("ix_appointments_status_scheduled", "status", "scheduled_at"),
        Index("ix_appointments_type_version", "appointment_type_version_id"),
        Index("ix_appointments_relationship", "patient_relationship_at_booking"),
    )


class AppointmentType(Base):
    """Stable identity for an administrator-defined appointment type (spec 18.3)."""
    __tablename__ = "appointment_types"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by_user_id = Column(Integer)  # future FK once auth exists
    is_system_seeded = Column(Boolean, default=False)
    active = Column(Boolean, default=True)
    versions = relationship("AppointmentTypeVersion", back_populates="appointment_type", cascade="all, delete-orphan")


class AppointmentTypeVersion(Base):
    """Immutable effective configuration for an appointment type (spec 18.3)."""
    __tablename__ = "appointment_type_versions"
    id = Column(Integer, primary_key=True, index=True)
    appointment_type_id = Column(Integer, ForeignKey("appointment_types.id"), nullable=False)
    version_number = Column(Integer, nullable=False)
    internal_name = Column(String, nullable=False)
    display_name = Column(String, nullable=False)
    calendar_abbreviation = Column(String, nullable=False)
    description = Column(Text)
    service_line = Column(String, nullable=False)
    display_order = Column(Integer, default=0)
    allows_new = Column(Boolean, default=True)
    allows_established = Column(Boolean, default=True)
    new_duration_minutes = Column(Integer)
    established_duration_minutes = Column(Integer)
    buffer_before_minutes = Column(Integer, default=0)
    buffer_after_minutes = Column(Integer, default=0)
    arrival_lead_minutes = Column(Integer, default=0)
    base_color = Column(String)
    staff_bookable = Column(Boolean, default=True)
    patient_bookable = Column(Boolean, default=False)
    effective_from = Column(String)
    effective_through = Column(String)
    active = Column(Boolean, default=True)
    change_reason = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by_user_id = Column(Integer)

    appointment_type = relationship("AppointmentType", back_populates="versions")
    color_rules = relationship("AppointmentTypeColorRule", back_populates="version", cascade="all, delete-orphan", order_by="AppointmentTypeColorRule.priority")
    resource_requirements = relationship("AppointmentTypeResourceRequirement", back_populates="version", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("appointment_type_id", "version_number", name="uq_apptype_version"),
        Index("ix_apptypeversion_active_effective", "appointment_type_id", "active", "effective_from"),
    )


class AppointmentTypeColorRule(Base):
    """Ordered conditional color rule (spec 9.3/9.5)."""
    __tablename__ = "appointment_type_color_rules"
    id = Column(Integer, primary_key=True, index=True)
    appointment_type_version_id = Column(Integer, ForeignKey("appointment_type_versions.id"), nullable=False)
    priority = Column(Integer, default=0)
    patient_relationship = Column(String)  # 'new' | 'established' | None (any)
    is_follow_up = Column(Boolean)  # True/False, or NULL meaning "don't care"
    minimum_countable_tests = Column(Integer)
    maximum_countable_tests = Column(Integer)
    color = Column(String, nullable=False)
    reason_code = Column(String)
    version = relationship("AppointmentTypeVersion", back_populates="color_rules")


class DiagnosticTest(Base):
    __tablename__ = "diagnostic_tests"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, nullable=False, unique=True)
    display_name = Column(String, nullable=False)
    calendar_abbreviation = Column(String, nullable=False)
    active = Column(Boolean, default=True)
    counts_toward_color = Column(Boolean, default=True)
    default_duration_minutes = Column(Integer)
    display_order = Column(Integer, default=0)


class AppointmentTest(Base):
    __tablename__ = "appointment_tests"
    id = Column(Integer, primary_key=True, index=True)
    appointment_id = Column(Integer, ForeignKey("appointments.id"), nullable=False)
    diagnostic_test_id = Column(Integer, ForeignKey("diagnostic_tests.id"), nullable=False)
    status = Column(String, default="active")  # 'active' | 'cancelled'
    counts_toward_color_snapshot = Column(Boolean, default=True)
    required_resource_id = Column(Integer, ForeignKey("resources.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by_user_id = Column(Integer)

    appointment = relationship("Appointment", back_populates="tests")
    diagnostic_test = relationship("DiagnosticTest")

    __table_args__ = (
        Index("ix_appointment_tests_appointment", "appointment_id"),
    )


class Resource(Base):
    __tablename__ = "resources"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, nullable=False, unique=True)
    display_name = Column(String, nullable=False)
    resource_class = Column(String, nullable=False)  # provider|technician|exam_lane|room|device|other
    exclusive = Column(Boolean, default=True)
    active = Column(Boolean, default=True)


class AppointmentTypeResourceRequirement(Base):
    __tablename__ = "appointment_type_resource_requirements"
    id = Column(Integer, primary_key=True, index=True)
    appointment_type_version_id = Column(Integer, ForeignKey("appointment_type_versions.id"), nullable=False)
    resource_id = Column(Integer, ForeignKey("resources.id"))
    resource_pool_code = Column(String)
    required = Column(Boolean, default=True)
    offset_minutes = Column(Integer, default=0)
    duration_minutes = Column(Integer)
    version = relationship("AppointmentTypeVersion", back_populates="resource_requirements")


class AppointmentResourceReservation(Base):
    __tablename__ = "appointment_resource_reservations"
    id = Column(Integer, primary_key=True, index=True)
    appointment_id = Column(Integer, ForeignKey("appointments.id"), nullable=False)
    resource_id = Column(Integer, ForeignKey("resources.id"), nullable=False)
    reserved_start_at = Column(DateTime, nullable=False)
    reserved_end_at = Column(DateTime, nullable=False)
    active = Column(Boolean, default=True)
    override_reason = Column(Text)
    appointment = relationship("Appointment", back_populates="resource_reservations")
    resource = relationship("Resource")

    __table_args__ = (
        Index("ix_resource_reservations_resource_window", "resource_id", "reserved_start_at", "reserved_end_at"),
    )


class AvailabilityTemplate(Base):
    __tablename__ = "availability_templates"
    id = Column(Integer, primary_key=True, index=True)
    resource_id = Column(Integer, ForeignKey("resources.id"), nullable=False)
    day_of_week = Column(Integer, nullable=False)  # 0=Monday .. 6=Sunday
    start_time = Column(String, nullable=False)  # 'HH:MM'
    end_time = Column(String, nullable=False)
    effective_from = Column(String)
    effective_through = Column(String)
    active = Column(Boolean, default=True)

    __table_args__ = (
        Index("ix_availability_templates_resource_day", "resource_id", "day_of_week"),
    )


class AvailabilityException(Base):
    __tablename__ = "availability_exceptions"
    id = Column(Integer, primary_key=True, index=True)
    resource_id = Column(Integer, ForeignKey("resources.id"), nullable=False)
    start_at = Column(DateTime, nullable=False)
    end_at = Column(DateTime, nullable=False)
    exception_type = Column(String, default="blocked")  # 'blocked' | 'extra_availability'
    reason = Column(String)

    __table_args__ = (
        Index("ix_availability_exceptions_resource_window", "resource_id", "start_at", "end_at"),
    )


class ProviderAvailabilityTemplate(Base):
    """A provider's recurring weekly working hours -- the provider-scoped
    counterpart to AvailabilityTemplate above, which is resource-scoped only
    (exam lanes/rooms/devices) and has no way to represent a provider's own
    hours. Kept as a separate table rather than adding a nullable provider_id
    to AvailabilityTemplate: that table's resource_id is NOT NULL, and this
    app's migration runner only ever does ADD COLUMN/CREATE TABLE IF NOT
    EXISTS, never a table rebuild, so loosening an existing NOT NULL
    constraint isn't a pattern available here. Powers the real open-slot
    availability search (ehr/services/scheduling.py's find_open_slots)."""
    __tablename__ = "provider_availability_templates"
    id = Column(Integer, primary_key=True, index=True)
    provider_id = Column(Integer, ForeignKey("providers.id"), nullable=False)
    day_of_week = Column(Integer, nullable=False)  # 0=Monday .. 6=Sunday
    start_time = Column(String, nullable=False)  # 'HH:MM'
    end_time = Column(String, nullable=False)
    effective_from = Column(String)
    effective_through = Column(String)
    active = Column(Boolean, default=True)

    __table_args__ = (
        Index("ix_provider_availability_templates_provider_day", "provider_id", "day_of_week"),
    )


class ProviderAvailabilityException(Base):
    """Provider-scoped counterpart to AvailabilityException above (e.g. a
    provider's one-off block for a meeting, vacation, or personal appointment)."""
    __tablename__ = "provider_availability_exceptions"
    id = Column(Integer, primary_key=True, index=True)
    provider_id = Column(Integer, ForeignKey("providers.id"), nullable=False)
    start_at = Column(DateTime, nullable=False)
    end_at = Column(DateTime, nullable=False)
    exception_type = Column(String, default="blocked")  # 'blocked' | 'extra_availability'
    reason = Column(String)

    __table_args__ = (
        Index("ix_provider_availability_exceptions_provider_window", "provider_id", "start_at", "end_at"),
    )


class PracticeClosure(Base):
    """Practice-wide Holidays/Closures (spec: Holidays/Closures admin screen).

    DESIGN NOTE: AvailabilityException is resource-scoped (resource_id is a
    required FK). Two schema options were considered for practice-wide closures:
    (a) fan a closure out into one AvailabilityException row per active Resource,
        or (b) make AvailabilityException.resource_id nullable (NULL = practice-wide)
        and update conflict checks to treat null-resource rows as blocking everyone.
    Neither was safe here: this app does not seed any Resource rows today, so (a)
    would silently create ZERO exception rows (and stay that way for any resource
    added later, unless closures were re-applied); and SQLite's ALTER TABLE cannot
    drop a NOT NULL constraint, so (b) would require rebuilding the whole table just
    to add one nullable column semantics change -- a much riskier migration than
    adding a small new table. A dedicated `practice_closures` table (added via the
    same idempotent migration runner, see ehr/db/migrations.py) is the smaller,
    safer change: it needs no destructive ALTER, has nothing to fall out of sync
    with the Resource table, and conflict-checking just checks "is this date
    closed?" directly instead of trying to route through unrelated resource rows.
    """
    __tablename__ = "practice_closures"
    id = Column(Integer, primary_key=True, index=True)
    closure_date = Column(String, nullable=False, unique=True)  # 'YYYY-MM-DD'
    label = Column(String, nullable=False)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class DailyClosing(Base):
    """Store Operations > Daily Closing reconciliation entries. One row per
    payment type per posting date. `calculated_amount` is always 0.0 for now --
    there is no real transaction ledger yet to compute expected totals against
    (see the Daily Closing page's inline note, matching how e.g. the patient
    context strip's Balance Due already labels an unimplemented figure)."""
    __tablename__ = "daily_closings"
    id = Column(Integer, primary_key=True, index=True)
    posting_date = Column(String, nullable=False)  # 'YYYY-MM-DD'
    payment_type = Column(String, nullable=False)
    calculated_amount = Column(Float, default=0.0)
    actual_amount = Column(Float, default=0.0)
    variance = Column(Float, default=0.0)
    explanation = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_daily_closings_posting_date", "posting_date"),
    )


class AppointmentAuditEvent(Base):
    __tablename__ = "appointment_audit_events"
    id = Column(Integer, primary_key=True, index=True)
    appointment_id = Column(Integer, ForeignKey("appointments.id"), nullable=False)
    event_type = Column(String, nullable=False)
    field_name = Column(String)
    old_value = Column(Text)
    new_value = Column(Text)
    reason = Column(Text)
    actor_user_id = Column(Integer)  # future FK; no auth yet so left null
    occurred_at = Column(DateTime, default=datetime.utcnow)
    appointment = relationship("Appointment", back_populates="audit_events")

    __table_args__ = (
        Index("ix_appt_audit_appt_time", "appointment_id", "occurred_at"),
    )


class AppointmentTypeAuditEvent(Base):
    __tablename__ = "appointment_type_audit_events"
    id = Column(Integer, primary_key=True, index=True)
    appointment_type_id = Column(Integer, ForeignKey("appointment_types.id"), nullable=False)
    appointment_type_version_id = Column(Integer, ForeignKey("appointment_type_versions.id"))
    event_type = Column(String, nullable=False)
    change_reason = Column(Text)
    actor_user_id = Column(Integer)
    occurred_at = Column(DateTime, default=datetime.utcnow)

class EyeExam(Base):
    __tablename__ = "eye_exams"
    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)
    provider_id = Column(Integer, ForeignKey("providers.id"), nullable=False)
    exam_date = Column(String, nullable=False)
    chief_complaint = Column(Text)
    od_sc = Column(String); os_sc = Column(String)
    od_cc = Column(String); os_cc = Column(String)
    iop_od = Column(Float); iop_os = Column(Float); iop_method = Column(String)
    cover_test = Column(String)
    sl_lids_od = Column(String); sl_lids_os = Column(String)
    sl_cornea_od = Column(String); sl_cornea_os = Column(String)
    sl_lens_od = Column(String); sl_lens_os = Column(String)
    fundus_disc_od = Column(String); fundus_disc_os = Column(String)
    fundus_macula_od = Column(String); fundus_macula_os = Column(String)
    fundus_vessels_od = Column(String); fundus_vessels_os = Column(String)
    fundus_periphery_od = Column(String); fundus_periphery_os = Column(String)
    assessment = Column(Text); plan = Column(Text)
    diagnosis_codes = Column(String); follow_up_weeks = Column(Integer)
    # Structured Refractive Assessment (VISION_EHR_DATA_STANDARDS_RESEARCH.md
    # 5.1), alongside the free-text assessment/diagnosis_codes above -- diagnosis
    # coding itself stays free-text (deferred terminology-server work, 4.4); these
    # are comma-delimited multi-value strings where noted, matching this app's
    # existing convention for other multi-choice fields (e.g. Refraction.refraction_type).
    refractive_diagnosis = Column(String)  # comma-delimited: Myopia, Hyperopia, Astigmatism, Presbyopia, Anisometropia, Emmetropia
    refractive_laterality = Column(String)  # OD / OS / OU
    refractive_stability = Column(String)  # Stable / Progressing / Improving
    refractive_secondary_findings = Column(String)  # comma-delimited: Amblyopia, Strabismus history, Cataract suspect, Suspect Glaucoma
    created_at = Column(DateTime, default=datetime.utcnow)
    patient = relationship("Patient", back_populates="eye_exams")
    provider = relationship("Provider", back_populates="eye_exams")
    refractions = relationship("Refraction", back_populates="exam", cascade="all, delete-orphan")
    prescriptions = relationship("Prescription", back_populates="exam")
    anterior_segment_assessments = relationship("AnteriorSegmentAssessment", back_populates="exam", cascade="all, delete-orphan")
    glaucoma_trackings = relationship("GlaucomaTracking", back_populates="exam", cascade="all, delete-orphan")
    binocular_vision_assessments = relationship("BinocularVisionAssessment", back_populates="exam", cascade="all, delete-orphan")

class Refraction(Base):
    __tablename__ = "refractions"
    id = Column(Integer, primary_key=True, index=True)
    exam_id = Column(Integer, ForeignKey("eye_exams.id"), nullable=False)
    # 'habitual' (patient's current glasses as worn in), 'manifest' (subjective
    # refinement), or 'cycloplegic' (post-dilation) -- the three-step refraction
    # matrix from the IHE General Eye Evaluation profile (see
    # VISION_EHR_DATA_STANDARDS_RESEARCH.md 4.2). One exam can have up to one
    # Refraction row per type; an exam is free to have any subset of the three.
    refraction_type = Column(String, default="manifest")
    od_sphere = Column(Float); od_cylinder = Column(Float); od_axis = Column(Integer)
    od_add = Column(Float); od_va = Column(String)
    os_sphere = Column(Float); os_cylinder = Column(Float); os_axis = Column(Integer)
    os_add = Column(Float); os_va = Column(String)
    exam = relationship("EyeExam", back_populates="refractions")

class AnteriorSegmentAssessment(Base):
    """Anterior Segment / Ocular Surface Disease (Dry Eye) structured Assessment
    & Plan (VISION_EHR_DATA_STANDARDS_RESEARCH.md 5.2), the second of five
    clinical dashboards reviewed in v2.9 -- built in v2.11. Unlike Refraction's
    three types, one exam is expected to have at most one row here; modeled as
    a child table (rather than columns on EyeExam) since this is a distinct
    encounter-scoped assessment, matching Refraction's existing exam_id-FK
    child-row shape."""
    __tablename__ = "anterior_segment_assessments"
    id = Column(Integer, primary_key=True, index=True)
    exam_id = Column(Integer, ForeignKey("eye_exams.id"), nullable=False)
    primary_diagnosis_code = Column(String)  # free-text, e.g. 'H04.123' -- same treatment as EyeExam.diagnosis_codes
    severity = Column(String)  # Mild / Moderate / Severe
    # Objective grading scale for all three *_od/_os pairs below: '0', '1+', '2+', '3+', '4+'
    conjunctival_injection_od = Column(String); conjunctival_injection_os = Column(String)
    corneal_staining_od = Column(String); corneal_staining_os = Column(String)
    mgd_expression_od = Column(String); mgd_expression_os = Column(String)  # Meibomian Gland Dysfunction
    tbut_seconds_od = Column(Integer); tbut_seconds_os = Column(Integer)  # Tear Break-Up Time
    schirmer_mm_od = Column(Integer); schirmer_mm_os = Column(Integer)
    plan_therapeutics = Column(String)  # comma-delimited: Preservative-Free Tears, Warm Compresses, Topical Steroid, Restasis/Xiidra
    follow_up_interval = Column(String)
    clinical_notes = Column(Text)
    exam = relationship("EyeExam", back_populates="anterior_segment_assessments")

class GlaucomaTracking(Base):
    """Posterior Segment & Glaucoma Tracking structured Assessment & Plan
    (VISION_EHR_DATA_STANDARDS_RESEARCH.md 5.3), the third of five clinical
    dashboards reviewed in v2.9 -- built in v2.16. Kept as an exam_id-FK child
    row, same shape as Refraction/AnteriorSegmentAssessment, rather than a
    separate patient-scoped table -- trending across visits (the one thing
    this dashboard wants that the other two don't) is achieved by querying
    every row across a patient's exam history (joined via EyeExam.patient_id),
    not by denormalizing patient_id onto this table."""
    __tablename__ = "glaucoma_trackings"
    id = Column(Integer, primary_key=True, index=True)
    exam_id = Column(Integer, ForeignKey("eye_exams.id"), nullable=False)
    primary_diagnosis_code = Column(String)  # free-text, e.g. 'H40.1132'
    target_iop_od = Column(Integer); target_iop_os = Column(Integer)  # mmHg
    iop_current_od = Column(Integer); iop_current_os = Column(Integer)  # mmHg
    iop_time_measured = Column(String)  # 'HH:MM' -- IOP varies by time of day
    iop_method = Column(String)  # Goldmann Applanation / Tono-Pen / iCare
    cup_disc_ratio_od = Column(Float); cup_disc_ratio_os = Column(Float)  # 0.00-1.00
    nerve_tissue_status_od = Column(String); nerve_tissue_status_os = Column(String)  # e.g. 'Healthy Rim', 'Inferior thinning'
    oct_rnfl_average_microns_od = Column(Integer); oct_rnfl_average_microns_os = Column(Integer)
    visual_field_md_db_od = Column(Float); visual_field_md_db_os = Column(Float)
    vf_reliability_od = Column(String); vf_reliability_os = Column(String)  # Reliable / Borderline / Unreliable
    prescribed_glaucoma_meds = Column(String)  # comma-delimited, e.g. 'Latanoprost 0.005% QHS OU'
    diagnostic_orders = Column(String)  # comma-delimited, e.g. 'OCT RNFL, Humphrey VF 24-2'
    follow_up_interval = Column(String)
    clinical_notes = Column(Text)
    exam = relationship("EyeExam", back_populates="glaucoma_trackings")

class BinocularVisionAssessment(Base):
    """Binocular Vision & Pediatrics (Vision Therapy) structured Assessment &
    Plan (VISION_EHR_DATA_STANDARDS_RESEARCH.md 5.4), the fourth of five
    clinical dashboards reviewed in v2.9 -- built in v2.17. Same exam_id-FK
    child-row shape as AnteriorSegmentAssessment/GlaucomaTracking (one row per
    exam) -- unlike Glaucoma, this dashboard's own field list has no
    longitudinal-trending ask beyond therapy_session_number, so it stays a
    single-visit-snapshot dashboard, no trend view."""
    __tablename__ = "binocular_vision_assessments"
    id = Column(Integer, primary_key=True, index=True)
    exam_id = Column(Integer, ForeignKey("eye_exams.id"), nullable=False)
    primary_diagnosis_code = Column(String)  # free-text, e.g. 'H51.11' (Convergence insufficiency)
    phoria_distance_diopters = Column(Integer); phoria_near_diopters = Column(Integer)  # Negative = Exo, Positive = Eso
    strabismus_present = Column(Boolean)
    strabismus_direction = Column(String)  # Exotropia / Esotropia / Hypertropia
    npc_break_cm = Column(Float); npc_recovery_cm = Column(Float)  # Near Point of Convergence
    accommodation_amplitude_od = Column(Float); accommodation_amplitude_os = Column(Float)  # Diopters
    assigned_home_exercises = Column(String)  # comma-delimited, e.g. 'Brock String, Lifesaver Card'
    therapy_session_number = Column(Integer)  # e.g. session 4 of 12
    therapy_compliance_rating = Column(String)  # Excellent / Good / Fair / Poor
    follow_up_interval = Column(String)
    clinical_notes = Column(Text)
    exam = relationship("EyeExam", back_populates="binocular_vision_assessments")

class Prescription(Base):
    __tablename__ = "prescriptions"
    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)
    exam_id = Column(Integer, ForeignKey("eye_exams.id"), nullable=True)
    provider_id = Column(Integer, ForeignKey("providers.id"), nullable=False)
    rx_type = Column(String, default="glasses")
    issue_date = Column(String); expiry_date = Column(String)
    od_sphere = Column(Float); od_cylinder = Column(Float); od_axis = Column(Integer)
    od_add = Column(Float); od_prism = Column(Float); od_base = Column(String)
    od_bc = Column(Float); od_dia = Column(Float); od_brand = Column(String)
    os_sphere = Column(Float); os_cylinder = Column(Float); os_axis = Column(Integer)
    os_add = Column(Float); os_prism = Column(Float); os_base = Column(String)
    os_bc = Column(Float); os_dia = Column(Float); os_brand = Column(String)
    notes = Column(Text)
    # Lens Design & Follow-Up plan fields (VISION_EHR_DATA_STANDARDS_RESEARCH.md
    # 5.1) -- not restricted to rx_type == "glasses", same non-restrictive
    # treatment the existing contact-lens fields already get on a glasses Rx.
    lens_type = Column(String)  # Single Vision / Bifocal / Trifocal / Progressive / Office-Computer
    lens_material = Column(String)  # CR-39 / Polycarbonate / Trivex / Hi-Index 1.67 / Hi-Index 1.74
    lens_treatments = Column(String)  # comma-delimited: Anti-Reflective Coating, Blue Light Filter, Transitions/Photochromic, Polarized
    recall_interval = Column(String)  # 3 Months / 6 Months / 1 Year / 2 Years
    patient_education_tags = Column(String)  # comma-delimited: 20-20-20 Rule, UV Protection, Contact Lens hygiene
    created_at = Column(DateTime, default=datetime.utcnow)
    patient = relationship("Patient", back_populates="prescriptions")
    exam = relationship("EyeExam", back_populates="prescriptions")
    provider = relationship("Provider", back_populates="prescriptions")

# ---------------------------------------------------------------------------
# Authentication / RBAC / auth-audit models (real-auth pass). See
# ehr/auth/security.py for the password-hashing scheme and ehr/auth/deps.py
# for the session-cookie mechanism and its expiry/timeout values.
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, nullable=False, unique=True, index=True)
    password_hash = Column(String, nullable=False)
    password_salt = Column(String, nullable=False)
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    # One of the 8 role constants in ehr/auth/permissions.py. Kept as a plain
    # String (not a SQLAlchemy Enum) so a new role can be added later without a
    # SQLite enum-migration headache -- validity is enforced at the application
    # layer (ehr/routes/auth.py checks against ALL_ROLES on create).
    role = Column(String, nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login_at = Column(DateTime)

    sessions = relationship("UserSession", back_populates="user", cascade="all, delete-orphan")


class UserSession(Base):
    __tablename__ = "user_sessions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    # Stored as the raw secrets.token_urlsafe(32) value with a unique index --
    # see ehr/auth/deps.py module docstring for why this is not hashed-at-rest.
    session_token = Column(String, nullable=False, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    revoked = Column(Boolean, default=False, nullable=False)

    user = relationship("User", back_populates="sessions")

    __table_args__ = (
        Index("ix_user_sessions_user_revoked", "user_id", "revoked"),
    )


class AuthAuditEvent(Base):
    """Authentication/access-control audit trail. SCOPE: login/logout/failed-
    login/session-expiry/access-denied/account-admin events ONLY -- distinct
    from appointment_audit_events / appointment_type_audit_events, which cover
    clinical/scheduling DATA changes. See ehr/auth/audit.py for the scope note.
    """
    __tablename__ = "auth_audit_events"
    id = Column(Integer, primary_key=True, index=True)
    event_type = Column(String, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    # Populated on login_failure when the attempted email does not match any
    # user (so there is no user_id to attach the event to) -- lets an admin
    # still see what was typed without ever needing to guess an account exists.
    actor_email_attempted = Column(String)
    ip_address = Column(String)
    occurred_at = Column(DateTime, default=datetime.utcnow)
    detail = Column(Text)

    __table_args__ = (
        Index("ix_auth_audit_occurred_at", "occurred_at"),
        Index("ix_auth_audit_user", "user_id"),
    )


def init_db():
    # NOTE: schema evolution for pre-existing tables (e.g. adding columns to the
    # long-lived `appointments` table) is handled by the versioned migration
    # runner in ehr/db/migrations.py, which must run BEFORE this. create_all()
    # here is safe/idempotent for tables that do not exist yet.
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

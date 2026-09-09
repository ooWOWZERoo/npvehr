import secrets
from datetime import datetime, timedelta
from ehr.models.database import (init_db, engine, SessionLocal, Provider, Patient, Appointment, EyeExam,
    Refraction, Prescription, AppointmentStatus, AppointmentType, AppointmentTypeVersion, User)
from ehr.db.migrations import run_column_migrations, run_post_create_all_migrations
from ehr.auth.security import hash_password
from ehr.auth.permissions import SYSTEM_ADMINISTRATOR, OPTOMETRIST_PROVIDER, FRONT_DESK, ROLE_LABELS


DEMO_PASSWORD = "ChangeMe123!"
# Fixed (not randomly regenerated per seed run) on purpose: these are throwaway
# local test/demo accounts only, wiped before any real production use -- a
# stable, known password is more useful for repeated local testing than a new
# random one every run. Still printed loudly below with a change-before-real-
# use warning, and never stored in plaintext (only its hash, via hash_password()).


def _seed_users(db):
    """Seed initial login accounts so the app is usable immediately after a
    fresh setup_ehr.py run. Idempotent: skipped entirely if any User already
    exists (mirrors the Provider-seeding idempotency check below)."""
    if db.query(User).count() > 0:
        return []
    now = datetime.utcnow()
    demo_accounts = [
        ("admin@newpathvision.example", "Ada", "Admin", SYSTEM_ADMINISTRATOR),
        ("schen@newpathvision.example", "Sarah", "Chen", OPTOMETRIST_PROVIDER),
        ("mrivera@newpathvision.example", "Marcus", "Rivera", OPTOMETRIST_PROVIDER),
        ("frontdesk@newpathvision.example", "Fran", "Desk", FRONT_DESK),
    ]
    created = []
    for email, first, last, role in demo_accounts:
        password = DEMO_PASSWORD
        salt_hex, hash_hex = hash_password(password)
        db.add(User(email=email, password_hash=hash_hex, password_salt=salt_hex,
                     first_name=first, last_name=last, role=role, active=True, created_at=now))
        created.append((email, password, role))
    return created


def seed():
    run_column_migrations(engine)
    init_db()
    run_post_create_all_migrations(engine)
    db = SessionLocal()

    # User accounts are seeded independently of Provider/Patient seeding below,
    # so an existing (pre-auth) database that already has patients/providers but
    # no users yet still gets usable login accounts on upgrade.
    created_users = _seed_users(db)
    db.commit()
    if created_users:
        print()
        print("=" * 60)
        print("  SEEDED LOGIN ACCOUNTS -- FOR LOCAL/DEMO USE ONLY")
        print("  Change these passwords before using with real patient data.")
        print("=" * 60)
        for email, password, role in created_users:
            print(f"    {ROLE_LABELS.get(role, role):<24} {email:<32} {password}")
        print("=" * 60)
        print()

    if db.query(Provider).count() > 0:
        print("Already seeded.")
        db.close()
        return
    p1 = Provider(first_name="Sarah", last_name="Chen", license_number="OD-12345", npi="1234567890")
    p2 = Provider(first_name="Marcus", last_name="Rivera", license_number="OD-67890", npi="0987654321")
    db.add_all([p1, p2]); db.flush()
    pts = [
        Patient(first_name="Alice", last_name="Johnson", date_of_birth="1985-03-15", gender="F",
                phone="555-0101", email="alice@example.com", address="123 Main St", city="Springfield",
                state="IL", zip_code="62701", insurance_provider="BlueCross", insurance_id="BC-111222",
                allergies="Penicillin", medical_history="Hypertension", ocular_history="Myopia since age 10"),
        Patient(first_name="Bob", last_name="Smith", date_of_birth="1972-07-22", gender="M",
                phone="555-0102", email="bob@example.com", city="Springfield", state="IL",
                insurance_provider="Aetna", insurance_id="AE-333444"),
        Patient(first_name="Carol", last_name="Davis", date_of_birth="1990-11-08", gender="F",
                phone="555-0103", email="carol@example.com", city="Springfield", state="IL",
                insurance_provider="United", insurance_id="UH-555666"),
        Patient(first_name="David", last_name="Wilson", date_of_birth="1968-05-30", gender="M",
                phone="555-0104", city="Springfield", state="IL"),
        Patient(first_name="Emma", last_name="Brown", date_of_birth="2005-01-12", gender="F",
                phone="555-0105", city="Springfield", state="IL",
                emergency_contact_name="Linda Brown", emergency_contact_phone="555-0200"),
    ]
    db.add_all(pts); db.flush()
    now = datetime.utcnow()

    def _current_version(code):
        t = db.query(AppointmentType).filter(AppointmentType.code == code).first()
        if not t:
            return None
        return (db.query(AppointmentTypeVersion)
                .filter(AppointmentTypeVersion.appointment_type_id == t.id, AppointmentTypeVersion.active == True)
                .order_by(AppointmentTypeVersion.version_number.desc()).first())

    comp_v = _current_version("COMP_VISION")
    cl_v = _current_version("CL_EVAL_CHECK")
    med_v = _current_version("MED_EYE_EVAL")

    def _mk(version, relationship, duration):
        return dict(appointment_type_version_id=version.id if version else None,
                    patient_relationship_at_booking=relationship, patient_relationship_source="automatic",
                    resolved_color=version.base_color if version else None,
                    resolved_color_reason="base_color_default", buffer_before_minutes=0, buffer_after_minutes=0,
                    duration_minutes=duration)

    db.add_all([
        Appointment(patient_id=pts[0].id, provider_id=p1.id, scheduled_at=now+timedelta(hours=2), reason="Annual exam",
                    **_mk(comp_v, "established", 20)),
        Appointment(patient_id=pts[1].id, provider_id=p1.id, scheduled_at=now+timedelta(hours=4), reason="Contact lens follow-up",
                    **_mk(cl_v, "established", 20)),
        Appointment(patient_id=pts[2].id, provider_id=p2.id, scheduled_at=now+timedelta(days=1), reason="New patient exam",
                    **_mk(comp_v, "new", 20)),
        Appointment(patient_id=pts[3].id, provider_id=p2.id, scheduled_at=now-timedelta(days=1), reason="Glaucoma check",
                    status=AppointmentStatus.completed, **_mk(med_v, "established", 20)),
    ]); db.flush()
    exam = EyeExam(
        patient_id=pts[0].id, provider_id=p1.id, exam_date="2026-09-01",
        chief_complaint="Blurry distance vision, needs new glasses",
        od_sc="20/200", os_sc="20/150", od_cc="20/20", os_cc="20/20",
        iop_od=14.0, iop_os=13.5, iop_method="Goldmann",
        cover_test="Orthophoria at distance and near",
        sl_lids_od="Normal", sl_lids_os="Normal", sl_cornea_od="Clear", sl_cornea_os="Clear",
        sl_lens_od="Clear", sl_lens_os="Clear",
        fundus_disc_od="0.3 C/D, sharp margins", fundus_disc_os="0.3 C/D, sharp margins",
        fundus_macula_od="Flat and even reflex", fundus_macula_os="Flat and even reflex",
        fundus_vessels_od="AV ratio 2/3", fundus_vessels_os="AV ratio 2/3",
        fundus_periphery_od="Flat, intact", fundus_periphery_os="Flat, intact",
        assessment="Myopia OU, stable. Astigmatism OU.", plan="New glasses prescription. RTC 1 year.",
        diagnosis_codes="H52.13, H52.219", follow_up_weeks=52,
    )
    db.add(exam); db.flush()
    db.add(Refraction(exam_id=exam.id, refraction_type="manifest",
        od_sphere=-3.25, od_cylinder=-0.75, od_axis=180, od_va="20/20",
        os_sphere=-2.50, os_cylinder=-0.50, os_axis=175, os_va="20/20"))
    db.add(Prescription(patient_id=pts[0].id, exam_id=exam.id, provider_id=p1.id,
        rx_type="glasses", issue_date="2026-09-01", expiry_date="2027-09-01",
        od_sphere=-3.25, od_cylinder=-0.75, od_axis=180,
        os_sphere=-2.50, os_cylinder=-0.50, os_axis=175))
    db.commit(); db.close()
    print("Seeded database.")

if __name__ == "__main__":
    seed()

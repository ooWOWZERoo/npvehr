import os, uuid
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, Request, Form, UploadFile, File, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from ehr.models.database import get_db, Patient, Appointment, EyeExam, Prescription, AppointmentStatus
from ehr.env_info import EHR_ENV
from ehr.utils import patient_context, compute_age, display_name
from ehr.auth.permissions import require_role, PATIENT_EDIT, ROLE_LABELS
from ehr.auth import csrf
from ehr.services.media import (save_patient_photo as _save_photo, delete_patient_photo as _delete_photo_file,
    get_photo_bytes as _get_photo_bytes)

router = APIRouter(prefix="/patients", tags=["patients"])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS

def _parse_balance(raw: str):
    """Parse the manually-entered balance_due form field. Blank -> None (even/no
    balance). Malformed input is discarded to None rather than raising, matching
    this app's existing convention for exam/prescription numeric fields."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None

@router.get("/", response_class=HTMLResponse)
def list_patients(request: Request, q: str = "",
    last_name: str = "", first_name: str = "", dob: str = "", phone: str = "", mrn: str = "",
    db: Session = Depends(get_db)):
    """Multi-field patient search (Last Name / First Name / DOB / Phone / MRN, AND-combined,
    partial+case-insensitive) with a legacy `q` fallback for the top-bar quick-switcher and any
    old bookmarks -- `q` alone still does the original OR-across-first/last/phone match."""
    query = db.query(Patient)
    any_field = last_name or first_name or dob or phone or mrn
    if any_field:
        if last_name:
            query = query.filter(Patient.last_name.ilike(f"%{last_name}%"))
        if first_name:
            query = query.filter(Patient.first_name.ilike(f"%{first_name}%"))
        if dob:
            query = query.filter(Patient.date_of_birth.ilike(f"%{dob}%"))
        if phone:
            query = query.filter(Patient.phone.ilike(f"%{phone}%"))
        if mrn:
            query = query.filter(Patient.mrn.ilike(f"%{mrn}%"))
    elif q:
        query = query.filter(
            (Patient.first_name.ilike(f"%{q}%")) |
            (Patient.last_name.ilike(f"%{q}%")) |
            (Patient.phone.ilike(f"%{q}%"))
        )
    patients = query.order_by(Patient.last_name).all()
    ages = {p.id: compute_age(p.date_of_birth) for p in patients}
    # NOTE: one query per patient row -- fine at seed-data scale, but would need a single
    # GROUP BY/eager-loaded query (or a denormalized "last_exam_date" column) at real scale.
    last_exam_dates = {}
    for p in patients:
        last = (db.query(EyeExam.exam_date).filter(EyeExam.patient_id == p.id)
                .order_by(EyeExam.exam_date.desc()).first())
        last_exam_dates[p.id] = last[0] if last else None
    return templates.TemplateResponse(request, "patients/list.html", {
        "patients": patients, "q": q,
        "last_name": last_name, "first_name": first_name, "dob": dob, "phone": phone, "mrn": mrn,
        "ages": ages, "last_exam_dates": last_exam_dates,
    })

@router.get("/merge", response_class=HTMLResponse)
def merge_patient_stub(request: Request):
    """Placeholder for duplicate-patient detection/merge (competitive review item).
    Not functional -- describes the eventual search/compare/merge workflow."""
    return templates.TemplateResponse(request, "placeholder.html", {
        "title": "Merge Patient",
        "icon": "&#128101;",
        "description": ("Merge Patient will help staff find and resolve duplicate patient records -- "
            "for example when the same person was registered twice under slightly different names, "
            "or with a typo'd date of birth. It will let you search for a suspected duplicate, review "
            "both records side by side, and combine them into one."),
        "bullets": [
            "A search screen to find likely duplicate patients by name, DOB, or phone.",
            "A side-by-side comparison view highlighting conflicting fields.",
            "A guided merge that consolidates appointments, exams, prescriptions, and history onto the surviving record.",
            "An audit trail recording which records were merged, when, and by whom.",
        ],
    })


@router.get("/search", response_class=JSONResponse)
def search_patients_json(q: str = "", db: Session = Depends(get_db)):
    """Lightweight JSON search backing the top-bar quick patient switcher's
    type-ahead. Kept separate from the HTML list view above (which reuses the
    same fields) so the widget gets a small, fast payload."""
    q = (q or "").strip()
    if not q:
        return JSONResponse([])
    results = (db.query(Patient)
               .filter((Patient.first_name.ilike(f"%{q}%")) |
                       (Patient.last_name.ilike(f"%{q}%")) |
                       (Patient.phone.ilike(f"%{q}%")))
               .order_by(Patient.last_name).limit(8).all())
    return JSONResponse([
        {"id": p.id, "name": display_name(p), "dob": p.date_of_birth or ""}
        for p in results
    ])


def _mrn_conflict(db: Session, mrn: str, exclude_patient_id=None):
    """Return the other patient already using this MRN, or None. Mirrors the partial
    UNIQUE index added in migration 010 (blank/None never conflicts) so the app can
    reject a duplicate with a friendly 400 instead of ever hitting a raw IntegrityError
    from the database constraint."""
    mrn = (mrn or "").strip()
    if not mrn:
        return None
    q = db.query(Patient).filter(Patient.mrn == mrn)
    if exclude_patient_id is not None:
        q = q.filter(Patient.id != exclude_patient_id)
    return q.first()

@router.get("/new", response_class=HTMLResponse)
def new_patient_form(request: Request):
    return templates.TemplateResponse(request, "patients/form.html", {"patient": None})

@router.post("/new", dependencies=[Depends(require_role(*PATIENT_EDIT))])
def create_patient(request: Request,
    first_name: str = Form(...), last_name: str = Form(...), preferred_name: str = Form(""),
    mrn: str = Form(""),
    date_of_birth: str = Form(""), gender: str = Form(""),
    phone: str = Form(""), email: str = Form(""),
    address: str = Form(""), city: str = Form(""), state: str = Form(""), zip_code: str = Form(""),
    insurance_provider: str = Form(""), insurance_id: str = Form(""),
    emergency_contact_name: str = Form(""), emergency_contact_phone: str = Form(""),
    allergies: str = Form(""), medical_history: str = Form(""),
    ocular_history: str = Form(""), family_ocular_history: str = Form(""),
    balance_due: str = Form(""),
    photo: UploadFile = File(None),
    csrf_token: str = Form(""),
    db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    conflict = _mrn_conflict(db, mrn)
    if conflict:
        pending = Patient(first_name=first_name, last_name=last_name, preferred_name=preferred_name or None,
            mrn=mrn, date_of_birth=date_of_birth or None, gender=gender, phone=phone, email=email,
            address=address, city=city, state=state, zip_code=zip_code,
            insurance_provider=insurance_provider, insurance_id=insurance_id,
            emergency_contact_name=emergency_contact_name, emergency_contact_phone=emergency_contact_phone,
            allergies=allergies, medical_history=medical_history, ocular_history=ocular_history,
            family_ocular_history=family_ocular_history, balance_due=_parse_balance(balance_due))
        return templates.TemplateResponse(request, "patients/form.html", {
            "patient": pending,
            "error": f"MRN \"{mrn.strip()}\" is already assigned to {display_name(conflict)} (patient #{conflict.id}). Each patient needs a unique MRN.",
        }, status_code=400)
    photo_path = _save_photo(photo)
    p = Patient(first_name=first_name, last_name=last_name, preferred_name=preferred_name or None,
        mrn=mrn or None, date_of_birth=date_of_birth or None,
        gender=gender, phone=phone, email=email, address=address, city=city,
        state=state, zip_code=zip_code, insurance_provider=insurance_provider,
        insurance_id=insurance_id, emergency_contact_name=emergency_contact_name,
        emergency_contact_phone=emergency_contact_phone, allergies=allergies,
        medical_history=medical_history, ocular_history=ocular_history,
        family_ocular_history=family_ocular_history, photo_path=photo_path,
        balance_due=_parse_balance(balance_due))
    db.add(p); db.commit(); db.refresh(p)
    return RedirectResponse(f"/patients/{p.id}", status_code=303)


# ---------------------------------------------------------------------------
# Patient workspace: identity header + secondary in-page sub-nav shared by every
# tab below. `_workspace_ctx` builds the common template context; each route
# adds its own tab-specific data on top of it.
# ---------------------------------------------------------------------------
def _get_patient_or_404(db: Session, patient_id: int):
    return db.query(Patient).filter(Patient.id == patient_id).first()

def _workspace_ctx(db: Session, p: Patient, active_tab: str) -> dict:
    # "Provider" in the identity header: no primary-provider field exists on Patient,
    # so this is best-effort -- the provider from the patient's most recent appointment,
    # falling back to '—' when there is no appointment history at all.
    last_appt = (db.query(Appointment).filter(Appointment.patient_id == p.id)
                 .order_by(Appointment.scheduled_at.desc()).first())
    provider_label = None
    if last_appt and last_appt.provider:
        provider_label = f"Dr. {last_appt.provider.last_name}"
    return {
        "patient": p,
        "context_patient": patient_context(p),
        "display_name": display_name(p),
        "age": compute_age(p.date_of_birth),
        "provider_label": provider_label,
        "active_tab": active_tab,
    }

@router.get("/{patient_id}", response_class=HTMLResponse)
def patient_detail(request: Request, patient_id: int, db: Session = Depends(get_db)):
    """Overview tab -- the pre-existing flat patient-detail URL, now rendering the
    Overview tab of the workspace restructure instead of the old flat page. Every
    existing link to this exact URL (patient list, back-links, context strip, the
    top-bar quick-switcher, recently-viewed) continues to work unchanged."""
    p = _get_patient_or_404(db, patient_id)
    if not p: return HTMLResponse("Not found", status_code=404)
    ctx = _workspace_ctx(db, p, "overview")
    now = datetime.utcnow()
    upcoming = (db.query(Appointment).filter(Appointment.patient_id == p.id,
                    Appointment.scheduled_at >= now, Appointment.status == AppointmentStatus.scheduled)
                .order_by(Appointment.scheduled_at).limit(5).all())
    recent_appts = (db.query(Appointment).filter(Appointment.patient_id == p.id)
                    .order_by(Appointment.scheduled_at.desc()).limit(5).all())
    recent_exams = sorted(p.eye_exams, key=lambda e: e.exam_date or "", reverse=True)[:5]
    recent_rx = sorted(p.prescriptions, key=lambda r: r.issue_date or "", reverse=True)[:5]
    ctx.update({
        "upcoming_appointments": upcoming,
        "recent_appointments": recent_appts,
        "recent_exams": recent_exams,
        "recent_prescriptions": recent_rx,
    })
    return templates.TemplateResponse(request, "patients/overview.html", ctx)

@router.get("/{patient_id}/photo")
def patient_photo(patient_id: int, db: Session = Depends(get_db)):
    """Serves a patient's photo bytes directly, gated by the same session
    auth as every other route in this router (applied at inclusion in
    ehr/app.py) -- rather than the browser holding a URL (Cloudinary or, in
    the original baseline, a local /static/uploads/ path) that works for
    anyone who has it, forever, with no auth check at all. Patient.photo_path
    itself never leaves the server as a browser-facing URL; templates point
    <img> tags at this route instead."""
    p = db.query(Patient).filter(Patient.id == patient_id).first()
    if not p or not p.photo_path:
        return HTMLResponse(status_code=404, content="")
    result = _get_photo_bytes(p.photo_path)
    if result is None:
        return HTMLResponse(status_code=404, content="")
    data, content_type = result
    return Response(content=data, media_type=content_type,
                     headers={"Cache-Control": "private, max-age=300"})

@router.get("/{patient_id}/edit", response_class=HTMLResponse)
def edit_patient_form(request: Request, patient_id: int, db: Session = Depends(get_db)):
    p = db.query(Patient).filter(Patient.id == patient_id).first()
    if not p: return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "patients/form.html",
        {"patient": p, "context_patient": patient_context(p)})

@router.post("/{patient_id}/edit", dependencies=[Depends(require_role(*PATIENT_EDIT))])
def update_patient(request: Request, patient_id: int,
    first_name: str = Form(...), last_name: str = Form(...), preferred_name: str = Form(""),
    mrn: str = Form(""),
    date_of_birth: str = Form(""), gender: str = Form(""),
    phone: str = Form(""), email: str = Form(""),
    address: str = Form(""), city: str = Form(""), state: str = Form(""), zip_code: str = Form(""),
    insurance_provider: str = Form(""), insurance_id: str = Form(""),
    emergency_contact_name: str = Form(""), emergency_contact_phone: str = Form(""),
    allergies: str = Form(""), medical_history: str = Form(""),
    ocular_history: str = Form(""), family_ocular_history: str = Form(""),
    balance_due: str = Form(""),
    photo: UploadFile = File(None),
    csrf_token: str = Form(""),
    db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    p = db.query(Patient).filter(Patient.id == patient_id).first()
    if not p: return HTMLResponse("Not found", status_code=404)
    conflict = _mrn_conflict(db, mrn, exclude_patient_id=patient_id)
    if conflict:
        pending = Patient(id=p.id, first_name=first_name, last_name=last_name,
            preferred_name=preferred_name or None, mrn=mrn, date_of_birth=date_of_birth or None,
            gender=gender, phone=phone, email=email, address=address, city=city, state=state,
            zip_code=zip_code, insurance_provider=insurance_provider, insurance_id=insurance_id,
            emergency_contact_name=emergency_contact_name, emergency_contact_phone=emergency_contact_phone,
            allergies=allergies, medical_history=medical_history, ocular_history=ocular_history,
            family_ocular_history=family_ocular_history, balance_due=_parse_balance(balance_due),
            photo_path=p.photo_path)
        return templates.TemplateResponse(request, "patients/form.html", {
            "patient": pending,
            "error": f"MRN \"{mrn.strip()}\" is already assigned to {display_name(conflict)} (patient #{conflict.id}). Each patient needs a unique MRN.",
        }, status_code=400)
    p.first_name=first_name; p.last_name=last_name; p.preferred_name=preferred_name or None
    p.mrn=mrn or None; p.date_of_birth=date_of_birth or None
    p.gender=gender; p.phone=phone; p.email=email; p.address=address
    p.city=city; p.state=state; p.zip_code=zip_code
    p.insurance_provider=insurance_provider; p.insurance_id=insurance_id
    p.emergency_contact_name=emergency_contact_name; p.emergency_contact_phone=emergency_contact_phone
    p.allergies=allergies; p.medical_history=medical_history
    p.ocular_history=ocular_history; p.family_ocular_history=family_ocular_history
    p.balance_due = _parse_balance(balance_due)
    new_photo_path = _save_photo(photo)
    if new_photo_path:
        old_photo_path = p.photo_path
        p.photo_path = new_photo_path
        db.commit()
        # Delete the old file only after the new path is safely committed, so a
        # mid-request failure never leaves the record pointing at a deleted file.
        _delete_photo_file(old_photo_path)
    else:
        db.commit()
    return RedirectResponse(f"/patients/{patient_id}", status_code=303)


def _placeholder_tab(request: Request, db: Session, patient_id: int, active_tab: str,
                      title: str, icon: str, description: str, bullets=None):
    p = _get_patient_or_404(db, patient_id)
    if not p: return HTMLResponse("Not found", status_code=404)
    ctx = _workspace_ctx(db, p, active_tab)
    ctx.update({"title": title, "icon": icon, "description": description, "bullets": bullets or []})
    return templates.TemplateResponse(request, "patients/workspace_placeholder.html", ctx)


@router.get("/{patient_id}/demographics", response_class=HTMLResponse)
def patient_demographics(request: Request, patient_id: int, db: Session = Depends(get_db)):
    p = _get_patient_or_404(db, patient_id)
    if not p: return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "patients/demographics.html", _workspace_ctx(db, p, "demographics"))


@router.get("/{patient_id}/addresses", response_class=HTMLResponse)
def patient_addresses(request: Request, patient_id: int, db: Session = Depends(get_db)):
    return _placeholder_tab(request, db, patient_id, "addresses", "Additional Addresses", "&#127968;",
        "Additional Addresses will let this patient have more than one address on file -- for example "
        "a home address, a separate billing address, and a work address -- instead of the single "
        "address this record supports today.",
        ["Add, label, and edit multiple addresses per patient (home, billing, work, other).",
         "Choose which address is used for statements/mailings vs. which is the primary contact address.",
         "Keep full history of prior addresses rather than overwriting the one address field."])


@router.get("/{patient_id}/appointments", response_class=HTMLResponse)
def patient_appointments(request: Request, patient_id: int, db: Session = Depends(get_db)):
    """Real tab -- reuses the same Appointment query/ordering as the global
    appointments list, just filtered by patient_id instead of duplicating logic."""
    p = _get_patient_or_404(db, patient_id)
    if not p: return HTMLResponse("Not found", status_code=404)
    ctx = _workspace_ctx(db, p, "appointments")
    ctx["appointments"] = (db.query(Appointment).filter(Appointment.patient_id == patient_id)
                            .order_by(Appointment.scheduled_at.desc()).all())
    return templates.TemplateResponse(request, "patients/appointments_tab.html", ctx)


@router.get("/{patient_id}/recalls", response_class=HTMLResponse)
def patient_recalls(request: Request, patient_id: int, db: Session = Depends(get_db)):
    return _placeholder_tab(request, db, patient_id, "recalls", "Recalls", "&#128276;",
        "Recalls will track when this patient is due back for a follow-up -- for example "
        "\"due back in 12 months for a comprehensive exam\" -- and let staff generate reminder "
        "lists tied into the existing appointment scheduling module.",
        ["Automatic due-back dates computed from exam type and provider-set recall intervals.",
         "A recall worklist staff can use to call/text/email patients who are due or overdue.",
         "Marking a recall satisfied automatically once the matching appointment is completed."])


@router.get("/{patient_id}/insurance", response_class=HTMLResponse)
def patient_insurance(request: Request, patient_id: int, db: Session = Depends(get_db)):
    p = _get_patient_or_404(db, patient_id)
    if not p: return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "patients/insurance_tab.html", _workspace_ctx(db, p, "insurance"))


@router.get("/{patient_id}/insurance/eligibility", response_class=HTMLResponse)
def patient_insurance_eligibility(request: Request, patient_id: int, db: Session = Depends(get_db)):
    return _placeholder_tab(request, db, patient_id, "insurance-eligibility", "Eligibility / Authorization", "&#9989;",
        "Eligibility / Authorization will let staff check this patient's insurance eligibility and "
        "track prior-authorization status directly from their record, instead of relying on a "
        "separate payer portal or phone call.",
        ["Real-time or batch eligibility checks against the insurance on file.",
         "A log of authorization requests, approvals, denials, and expiration dates.",
         "Alerts when an authorization is missing or about to expire ahead of a scheduled visit."])


@router.get("/{patient_id}/insurance/relationships", response_class=HTMLResponse)
def patient_insurance_relationships(request: Request, patient_id: int, db: Session = Depends(get_db)):
    return _placeholder_tab(request, db, patient_id, "insurance-relationships", "Relationships", "&#128106;",
        "Relationships will let family/dependent patient records be linked together -- for example "
        "a child's record linked to a parent's, so the parent's insurance can be used to cover the "
        "dependent's visit -- instead of every patient record being fully independent as it is today.",
        ["Link a dependent patient record to a subscriber/guarantor patient record.",
         "Reuse the subscriber's insurance information on the dependent's claims.",
         "See linked family members from any one member's patient record."])


@router.get("/{patient_id}/rx", response_class=HTMLResponse)
def patient_rx(request: Request, patient_id: int, db: Session = Depends(get_db)):
    """Real tab -- reuses the existing Prescription data for this patient."""
    p = _get_patient_or_404(db, patient_id)
    if not p: return HTMLResponse("Not found", status_code=404)
    ctx = _workspace_ctx(db, p, "rx")
    ctx["prescriptions"] = sorted(p.prescriptions, key=lambda r: r.issue_date or "", reverse=True)
    return templates.TemplateResponse(request, "patients/rx_tab.html", ctx)


@router.get("/{patient_id}/rx/glasses", response_class=HTMLResponse)
def patient_rx_glasses(request: Request, patient_id: int, db: Session = Depends(get_db)):
    p = _get_patient_or_404(db, patient_id)
    if not p: return HTMLResponse("Not found", status_code=404)
    ctx = _workspace_ctx(db, p, "rx-glasses")
    ctx["prescriptions"] = sorted(
        [r for r in p.prescriptions if r.rx_type == "glasses"], key=lambda r: r.issue_date or "", reverse=True)
    ctx["rx_kind_label"] = "Glasses"
    return templates.TemplateResponse(request, "patients/rx_tab.html", ctx)


@router.get("/{patient_id}/rx/contacts", response_class=HTMLResponse)
def patient_rx_contacts(request: Request, patient_id: int, db: Session = Depends(get_db)):
    p = _get_patient_or_404(db, patient_id)
    if not p: return HTMLResponse("Not found", status_code=404)
    ctx = _workspace_ctx(db, p, "rx-contacts")
    ctx["prescriptions"] = sorted(
        [r for r in p.prescriptions if r.rx_type == "contacts"], key=lambda r: r.issue_date or "", reverse=True)
    ctx["rx_kind_label"] = "Contact Lenses"
    return templates.TemplateResponse(request, "patients/rx_tab.html", ctx)


@router.get("/{patient_id}/orders", response_class=HTMLResponse)
def patient_orders(request: Request, patient_id: int, db: Session = Depends(get_db)):
    return _placeholder_tab(request, db, patient_id, "orders", "Material Orders", "&#128230;",
        "Material Orders will track this patient's optical orders (glasses and contact lens orders) "
        "end to end -- from order placement through lab/vendor fulfillment to patient pickup -- "
        "consistent with the practice-wide Orders module (see Store Operations &raquo; Order Management).",
        ["A per-patient order history separate from the practice-wide order queue.",
         "Order status tracking (ordered, in lab, arrived, ready for pickup, dispensed).",
         "Links back to the eyeglass/contact lens Rx and exam that generated each order.",
         "See also the global &ldquo;Order Management&rdquo; stub under Orders in the main sidebar."])


@router.get("/{patient_id}/orders/exams", response_class=HTMLResponse)
def patient_orders_exams(request: Request, patient_id: int, db: Session = Depends(get_db)):
    """INTERPRETATION NOTE: Encompass's \"Material Orders > Exams\" most plausibly refers to
    exam-related billing/order records (e.g. an order line for the exam itself), not the clinical
    eye-exam chart -- but this app has no billing/order-line concept for exams at all, only the
    clinical EyeExam record. Rather than fabricate a fake billing order screen, this reuses the real,
    already-existing per-patient exam history (same data as the Overview/Exams area) since that is the
    closest real, honest thing to show under this tab; a future billing/orders module should replace
    this with the actual exam-order records Encompass models."""
    p = _get_patient_or_404(db, patient_id)
    if not p: return HTMLResponse("Not found", status_code=404)
    ctx = _workspace_ctx(db, p, "orders-exams")
    ctx["exams"] = sorted(p.eye_exams, key=lambda e: e.exam_date or "", reverse=True)
    return templates.TemplateResponse(request, "patients/orders_exams_tab.html", ctx)


def _build_iop_trend(rows_oldest_first):
    """rows_oldest_first: list of (EyeExam, GlaucomaTracking) tuples, oldest
    exam first, already filtered to rows with at least one current-IOP value.
    Returns ready-made SVG point-string data for a simple inline line chart
    (no charting library, no CDN dependency -- matches this app's
    zero-external-JS-dependency convention), or None if nothing to plot.
    Points are index-spaced on the X axis since visit dates aren't evenly
    distributed; target IOP is shown in the accompanying table, not layered
    onto the chart, to keep the one visual signal (current IOP trend) clear."""
    if not rows_oldest_first:
        return None
    width, height, pad_l, pad_r, pad_t, pad_b = 640, 220, 10, 10, 10, 10
    plot_w, plot_h, y_max = width - pad_l - pad_r, height - pad_t - pad_b, 40  # mmHg chart ceiling
    n = len(rows_oldest_first)
    step = plot_w / (n - 1) if n > 1 else 0
    def x_at(i): return pad_l + i * step
    def y_at(v): return pad_t + plot_h * (1 - min(v, y_max) / y_max)
    od_points, os_points = [], []
    for i, (exam, gt) in enumerate(rows_oldest_first):
        x = x_at(i)
        if gt.iop_current_od is not None:
            od_points.append(f"{x:.1f},{y_at(gt.iop_current_od):.1f}")
        if gt.iop_current_os is not None:
            os_points.append(f"{x:.1f},{y_at(gt.iop_current_os):.1f}")
    return {"width": width, "height": height, "od_points": " ".join(od_points),
            "os_points": " ".join(os_points), "gridline_y": round(y_at(21), 1)}  # 21 mmHg: common upper-normal reference


@router.get("/{patient_id}/glaucoma-trend", response_class=HTMLResponse)
def patient_glaucoma_trend(request: Request, patient_id: int, db: Session = Depends(get_db)):
    """Posterior Segment / Glaucoma Tracking's longitudinal view
    (VISION_EHR_DATA_STANDARDS_RESEARCH.md 5.3) -- the one dashboard of the
    five that wants trending across visits, unlike the single-visit-snapshot
    shape used elsewhere. GlaucomaTracking stays exam-scoped (same as
    Refraction/AnteriorSegmentAssessment); this route just walks a patient's
    exam history collecting each exam's tracking row, rather than the table
    itself carrying a redundant patient_id."""
    p = _get_patient_or_404(db, patient_id)
    if not p: return HTMLResponse("Not found", status_code=404)
    ctx = _workspace_ctx(db, p, "glaucoma-trend")
    exams_oldest_first = sorted(p.eye_exams, key=lambda e: e.exam_date or "")
    rows_oldest_first = [(e, e.glaucoma_trackings[0]) for e in exams_oldest_first if e.glaucoma_trackings]
    ctx["gt_rows"] = list(reversed(rows_oldest_first))  # newest first for the table
    ctx["chart"] = _build_iop_trend([r for r in rows_oldest_first if r[1].iop_current_od is not None or r[1].iop_current_os is not None])
    return templates.TemplateResponse(request, "patients/glaucoma_trend_tab.html", ctx)


@router.get("/{patient_id}/surgery-timeline", response_class=HTMLResponse)
def patient_surgery_timeline(request: Request, patient_id: int, db: Session = Depends(get_db)):
    """Pre-/Post-Operative Co-Management's longitudinal view
    (VISION_EHR_DATA_STANDARDS_RESEARCH.md 5.5), the fifth and last of five
    clinical dashboards. The source document models this as one row per
    follow-up visit along a timeline (Pre-Op -> Day 1 -> Week 1 -> ...); that
    is naturally satisfied here since SurgeryComanagementTracking stays
    exam-scoped like the others -- this route just walks a patient's exam
    history collecting each exam's tracking row, same shape as
    patient_glaucoma_trend above, minus the chart (milestones are
    categorical, not a quantity worth trending visually -- the ordered table
    itself is the timeline)."""
    p = _get_patient_or_404(db, patient_id)
    if not p: return HTMLResponse("Not found", status_code=404)
    ctx = _workspace_ctx(db, p, "surgery-timeline")
    exams_oldest_first = sorted(p.eye_exams, key=lambda e: e.exam_date or "")
    rows_oldest_first = [(e, e.surgery_comanagement_trackings[0]) for e in exams_oldest_first if e.surgery_comanagement_trackings]
    ctx["sx_rows"] = list(reversed(rows_oldest_first))  # newest first for the table
    return templates.TemplateResponse(request, "patients/surgery_timeline_tab.html", ctx)


@router.get("/{patient_id}/orders/eyeglass", response_class=HTMLResponse)
def patient_orders_eyeglass(request: Request, patient_id: int, db: Session = Depends(get_db)):
    return _placeholder_tab(request, db, patient_id, "orders-eyeglass", "Eyeglass Order", "&#128083;",
        "Eyeglass Order will track this patient's optical lab orders for glasses -- frame, lens type, "
        "coatings, lab, and order status -- linked to the Rx that generated the order.",
        ["Capture frame, lens, and coating selections per order.",
         "Send/track the order with the optical lab and record its status.",
         "Link each order back to the Rx (Glasses) and exam it came from."])


@router.get("/{patient_id}/orders/contacts", response_class=HTMLResponse)
def patient_orders_contacts(request: Request, patient_id: int, db: Session = Depends(get_db)):
    return _placeholder_tab(request, db, patient_id, "orders-contacts", "Contact Lens Order", "&#128065;",
        "Contact Lens Order will track this patient's contact lens orders -- brand, parameters, "
        "quantity/supply, vendor, and order status -- linked to the Rx that generated the order.",
        ["Capture brand, parameters, and supply/quantity per order.",
         "Send/track the order with the vendor and record its status.",
         "Link each order back to the Rx (Contact Lenses) and exam it came from."])


@router.get("/{patient_id}/correspondence", response_class=HTMLResponse)
def patient_correspondence(request: Request, patient_id: int, db: Session = Depends(get_db)):
    return _placeholder_tab(request, db, patient_id, "correspondence", "Correspondence", "&#128231;",
        "Correspondence will track communications and documents sent to or received from this "
        "patient -- letters, emails, forms, and outside records -- in one place on their record.",
        ["A chronological log of correspondence sent/received for this patient.",
         "See Documents, an external records vault, and Notes below for specific planned areas."])


@router.get("/{patient_id}/correspondence/documents", response_class=HTMLResponse)
def patient_correspondence_documents(request: Request, patient_id: int, db: Session = Depends(get_db)):
    return _placeholder_tab(request, db, patient_id, "correspondence-documents", "Documents", "&#128196;",
        "Documents will support general document upload/storage for this patient -- signed forms, "
        "outside records, referral letters, and the like. This is explicitly distinct from the "
        "single profile photo this app already supports: today a patient record can hold exactly "
        "one photo and nothing else; there is no general document storage yet.",
        ["Upload and store multiple documents of any type per patient.",
         "Categorize documents (consent forms, outside records, correspondence, etc.).",
         "View/download stored documents from the patient record."])


@router.get("/{patient_id}/correspondence/notes", response_class=HTMLResponse)
def patient_correspondence_notes(request: Request, patient_id: int, db: Session = Depends(get_db)):
    return _placeholder_tab(request, db, patient_id, "correspondence-notes", "Notes", "&#128221;",
        "Notes will support free-form patient notes and a communication log -- day-to-day notes "
        "staff want on file (a call back request, a billing note, a front-desk reminder) -- distinct "
        "from the clinical Allergies / Medical History / Ocular History fields already on this "
        "patient's Demographics tab, which are clinical-history fields, not a running log.",
        ["Add free-form, timestamped notes to a patient's record.",
         "See who added each note and when (once user accounts exist).",
         "Keep this log clearly separate from clinical history fields."])

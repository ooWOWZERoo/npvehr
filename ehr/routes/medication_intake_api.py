"""REST API behind the guided medication intake (design: PATIENT_MEDICATION_LIST_DESIGN.md section 9). The intake UI
(static/js/med_intake.js) consumes exactly these endpoints and nothing else -- it never reads the database or builds SQL
-- and every rule (minimum search length, dosage-belongs-to-drug, PO only for oral drugs) is enforced here, not in the page.

All responses are JSON. Errors use FastAPI's {"detail": ...} shape: 401 not signed in, 403 role or CSRF, 404 unknown
drug / patient outside the caller's access, 422 validation. Mutations need the session CSRF token in an X-CSRF-Token header.

    GET  /api/v1/drugs/search?q=<text>              minimum 3 characters; matches names and brand aliases ("via_brand")
    GET  /api/v1/drugs/{drug_id}/dosages            first use of an RxNorm drug fetches its dosages (services/rxnorm.py); see "lookup"
    GET  /api/v1/drugs/{drug_id}/frequencies        drug's own route group first
    POST /api/v1/patients/{patient_id}/medications  add a medication (catalog pick or custom entry); MEDICATION_EDIT only
    GET  /api/v1/patients/{patient_id}/medications/{med_id}   one medication with its catalog state, to pre-fill an edit
    PUT  /api/v1/patients/{patient_id}/medications/{med_id}   re-save it through the same rules as POST; MEDICATION_EDIT only
"""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ehr.auth import csrf
from ehr.auth.deps import get_current_user
from ehr.auth.permissions import MEDICATION_EDIT, require_role
from ehr.models.database import get_db
from ehr.models.medications import PatientMedication
from ehr.routes.patients import _get_patient_or_404
from ehr.services import drug_catalog as catalog
from ehr.services.drug_catalog import Hit
from ehr.services import rxnorm
from ehr.services import medications as med_svc

router = APIRouter(prefix="/api/v1", tags=["medication-intake"])
NO_STORE = {"Cache-Control": "no-store"}


class DrugOut(BaseModel):
    id: int
    name: str
    rxcui: Optional[str] = None
    route: Optional[str] = None
    route_group: Optional[str] = None
    is_oral: bool
    via_brand: Optional[str] = None            # the brand name that found this generic (e.g. "Coumadin" -> warfarin)


class DosageOut(BaseModel):
    id: int
    label: str
    dose_form: Optional[str] = None
    route: Optional[str] = None
    is_oral: bool


class FrequencyOut(BaseModel):
    id: int
    label: str
    route_group: str
    preferred: bool


class SearchOut(BaseModel):
    query: str
    results: list[DrugOut]


class DosagesOut(BaseModel):
    drug_id: int
    dosages: list[DosageOut]
    # local = nothing to look up; ok = fetched from RxNorm just now; not_found = RxNorm has no entry for this name;
    # unavailable = RxNorm could not be reached (nothing is cached, so the next pick retries)
    lookup: Literal["local", "ok", "not_found", "unavailable"] = "local"


class FrequenciesOut(BaseModel):
    drug_id: int
    preferred_group: Optional[str] = None
    frequencies: list[FrequencyOut]


class MedicationIn(BaseModel):
    is_custom: bool = False
    # catalog pick
    drug_id: Optional[int] = None
    dosage_id: Optional[int] = None
    frequency_id: Optional[int] = None
    # custom entry (also carries the typed name)
    name: Optional[str] = Field(default=None, max_length=160)
    dosage_text: Optional[str] = Field(default=None, max_length=80)
    frequency_text: Optional[str] = Field(default=None, max_length=80)
    eye: Literal["OU", "OD", "OS", "PO", "NA", ""] = ""
    indication: Optional[str] = Field(default=None, max_length=160)
    note: Optional[str] = Field(default=None, max_length=255)
    start_date: Optional[str] = None


class MedicationOut(BaseModel):
    id: int
    patient_id: int
    name: str
    strength: Optional[str]
    frequency: Optional[str]
    eye: Optional[str]
    route: Optional[str]
    is_custom: bool
    drug_id: Optional[int]


class MedicationDetail(BaseModel):
    id: int
    patient_id: int
    status: str
    name: str
    strength: Optional[str]
    frequency: Optional[str]
    eye: Optional[str]
    route: Optional[str]
    indication: Optional[str]
    note: Optional[str]
    start_date: Optional[str]
    as_custom: bool                            # True for custom entries and for older free-text rows: edit them as free text
    drug: Optional[DrugOut] = None
    dosage_id: Optional[int] = None
    frequency_id: Optional[int] = None


def _drug_out(hit) -> DrugOut:
    d = hit.drug
    g = catalog.route_group(d.default_route)
    return DrugOut(id=d.id, name=d.name, rxcui=d.rxcui, route=d.default_route, route_group=g, is_oral=g == "oral", via_brand=hit.via)


def _json(model: BaseModel, status_code=200):
    return JSONResponse(model.model_dump(), status_code=status_code, headers=NO_STORE)


def _bad(msg, status_code=422):
    return JSONResponse({"detail": msg}, status_code=status_code, headers=NO_STORE)


@router.get("/drugs/search")
def search_drugs(q: str = "", db: Session = Depends(get_db), user=Depends(get_current_user)):
    try:
        rows = catalog.search(db, q)
    except ValueError as e:
        return _bad(str(e))
    if not rows:                                   # nothing local: maybe a brand name RxNorm can map to its generic
        rows = rxnorm.brand_fallback(db, q)
        if rows:
            db.commit()
    return _json(SearchOut(query=q.strip(), results=[_drug_out(h) for h in rows]))


@router.get("/drugs/{drug_id}/dosages")
def drug_dosages(drug_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    drug = catalog.get_drug(db, drug_id)
    if not drug:
        return _bad("Drug not found.", 404)
    lookup = rxnorm.ensure_resolved(db, drug)
    if lookup != "local":
        db.commit()
    return _json(DosagesOut(drug_id=drug.id, lookup=lookup, dosages=[
        DosageOut(id=x.id, label=x.label, dose_form=x.dose_form, route=catalog.effective_route(drug, x),
                  is_oral=catalog.is_oral(drug, x)) for x in catalog.dosages_for(db, drug)]))


@router.get("/drugs/{drug_id}/frequencies")
def drug_frequencies(drug_id: int, dosage_id: Optional[int] = None, db: Session = Depends(get_db), user=Depends(get_current_user)):
    drug = catalog.get_drug(db, drug_id)
    if not drug:
        return _bad("Drug not found.", 404)
    dosage = next((x for x in catalog.dosages_for(db, drug) if x.id == dosage_id), None)
    rows, group = catalog.frequencies_for(db, drug, dosage)
    pref = {group, "any"} if group else set()
    return _json(FrequenciesOut(drug_id=drug.id, preferred_group=group, frequencies=[
        FrequencyOut(id=f.id, label=f.label, route_group=f.route_group, preferred=f.route_group in pref) for f in rows]))


@router.post("/patients/{patient_id}/medications", dependencies=[Depends(get_current_user), Depends(require_role(*MEDICATION_EDIT))])
def add_patient_medication(request: Request, patient_id: int, body: MedicationIn, db: Session = Depends(get_db),
                           user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, request.headers.get("x-csrf-token"))
    if not _get_patient_or_404(db, patient_id, request.state.user):
        return _bad("Patient not found.", 404)
    try:
        values = catalog.resolve_selection(db, body)
    except ValueError as e:
        return _bad(str(e))
    m = med_svc.add_resolved_medication(db, patient_id, user.id, values, body.indication, body.note, body.start_date)
    db.commit()
    return _json(MedicationOut(id=m.id, patient_id=patient_id, name=m.name, strength=m.strength, frequency=m.frequency,
                               eye=m.eye, route=m.route, is_custom=m.is_custom, drug_id=m.drug_id), 201)


def _patient_med(db, request, patient_id, med_id):
    if not _get_patient_or_404(db, patient_id, request.state.user):
        return None
    m = db.get(PatientMedication, med_id)
    return m if m and m.patient_id == patient_id else None


@router.get("/patients/{patient_id}/medications/{med_id}")
def get_patient_medication(request: Request, patient_id: int, med_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    m = _patient_med(db, request, patient_id, med_id)
    if not m:
        return _bad("Medication not found.", 404)
    drug = catalog.get_drug(db, m.drug_id) if m.drug_id else None
    return _json(MedicationDetail(
        id=m.id, patient_id=patient_id, status=m.status, name=m.name, strength=m.strength, frequency=m.frequency, eye=m.eye, route=m.route,
        indication=m.indication, note=m.note, start_date=m.start_date.isoformat() if m.start_date else None,
        as_custom=bool(m.is_custom or not drug), drug=_drug_out(Hit(drug)) if drug else None, dosage_id=m.dosage_id, frequency_id=m.frequency_id))


@router.put("/patients/{patient_id}/medications/{med_id}", dependencies=[Depends(get_current_user), Depends(require_role(*MEDICATION_EDIT))])
def update_patient_medication(request: Request, patient_id: int, med_id: int, body: MedicationIn, db: Session = Depends(get_db),
                              user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, request.headers.get("x-csrf-token"))
    m = _patient_med(db, request, patient_id, med_id)
    if not m:
        return _bad("Medication not found.", 404)
    try:
        values = catalog.resolve_selection(db, body)
    except ValueError as e:
        return _bad(str(e))
    med_svc.update_resolved_medication(db, m, user.id, values, body.indication, body.note, body.start_date)
    db.commit()
    return _json(MedicationOut(id=m.id, patient_id=patient_id, name=m.name, strength=m.strength, frequency=m.frequency,
                               eye=m.eye, route=m.route, is_custom=m.is_custom, drug_id=m.drug_id))

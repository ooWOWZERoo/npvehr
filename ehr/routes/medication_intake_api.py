"""REST API behind the guided medication intake (design: PATIENT_MEDICATION_LIST_DESIGN.md section 9). The intake UI
(static/js/med_intake.js) consumes exactly these endpoints and nothing else -- it never reads the database or builds SQL
-- and every rule (minimum search length, dosage-belongs-to-drug, PO only for oral drugs) is enforced here, not in the page.

All responses are JSON. Errors use FastAPI's {"detail": ...} shape: 401 not signed in, 403 role or CSRF, 404 unknown
drug / patient outside the caller's access, 422 validation. Mutations need the session CSRF token in an X-CSRF-Token header.

    GET  /api/v1/drugs/search?q=<text>              minimum 3 characters
    GET  /api/v1/drugs/{drug_id}/dosages
    GET  /api/v1/drugs/{drug_id}/frequencies        drug's own route group first
    POST /api/v1/patients/{patient_id}/medications  add a medication (catalog pick or custom entry); MEDICATION_EDIT only
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
from ehr.routes.patients import _get_patient_or_404
from ehr.services import drug_catalog as catalog
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
    eye: Literal["OU", "OD", "OS", "PO", ""] = ""
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


def _drug_out(d) -> DrugOut:
    g = catalog.route_group(d.default_route)
    return DrugOut(id=d.id, name=d.name, rxcui=d.rxcui, route=d.default_route, route_group=g, is_oral=g == "oral")


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
    return _json(SearchOut(query=q.strip(), results=[_drug_out(d) for d in rows]))


@router.get("/drugs/{drug_id}/dosages")
def drug_dosages(drug_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    drug = catalog.get_drug(db, drug_id)
    if not drug:
        return _bad("Drug not found.", 404)
    return _json(DosagesOut(drug_id=drug.id, dosages=[
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

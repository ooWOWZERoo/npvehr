"""Drug catalog lookups and the guided-intake validation (design: PATIENT_MEDICATION_LIST_DESIGN.md section 9).

Everything the medication intake combobox needs lives here, so routes stay thin and the UI never touches the database:

    search(db, q)                  -> catalog rows matching what the technician typed (min 3 characters, prefix matches first)
    dosages_for(db, drug)          -> that drug's dosages
    frequencies_for(db, drug)      -> the frequency pick list, the drug's own route group first
    resolve_selection(db, payload) -> validated, normalised values for a new patient medication, or ValueError(message)

Route handling: a route string is reduced to one of ophthalmic / oral / topical (or None when unknown) by `route_group`.
An ORAL drug always records eye = 'PO' (by mouth / systemic) and route 'oral'; 'PO' is refused for a drug whose route is
ophthalmic or topical, since those are applied to an eye or the skin. A custom (free-text) entry may use any eye value.
Nothing here sends anything outside the app: the catalog is rows in our own database.
"""
import re
from typing import Optional

from sqlalchemy import case
from sqlalchemy.orm import Session

from ehr.models.drug_catalog import DrugCatalogEntry, DrugDosage, DrugFrequency

MIN_QUERY_CHARS = 3
MAX_RESULTS = 15
EYE_CHOICES = ("OU", "OD", "OS", "PO")
GROUP_ORDER = ("ophthalmic", "oral", "topical", "any")


def route_group(route: Optional[str]) -> Optional[str]:
    """'Ophthalmic Solution' / 'ophthalmic' -> 'ophthalmic'; 'Oral Tablet' / 'oral' / 'by mouth' -> 'oral'; unknown -> None."""
    r = (route or "").lower()
    if re.search(r"ophthalm|intraocular|\beye\b|conjunctival", r):
        return "ophthalmic"
    if re.search(r"\boral\b|by mouth|\bpo\b|sublingual|buccal", r):
        return "oral"
    if re.search(r"topical|cutaneous|transdermal|dermal|\bcream\b|\bointment\b", r):
        return "topical"
    return None


def effective_route(drug: DrugCatalogEntry, dosage: Optional[DrugDosage] = None) -> Optional[str]:
    """The dosage's own route when it has one, otherwise the drug's usual route."""
    return (dosage.route if dosage and dosage.route else None) or drug.default_route


def is_oral(drug: DrugCatalogEntry, dosage: Optional[DrugDosage] = None) -> bool:
    return route_group(effective_route(drug, dosage)) == "oral"


def _like_escape(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def search(db: Session, q: str) -> list:
    """Active catalog rows whose name starts with, or otherwise contains, `q` (case-insensitive). Prefix matches sort
    first, then alphabetically. Raises ValueError below the minimum length so a caller can't dump the catalog."""
    q = (q or "").strip().lower()
    if len(q) < MIN_QUERY_CHARS:
        raise ValueError(f"Type at least {MIN_QUERY_CHARS} characters to search.")
    like = _like_escape(q)
    starts = DrugCatalogEntry.name_lower.like(like + "%", escape="\\")
    return (db.query(DrugCatalogEntry)
            .filter(DrugCatalogEntry.is_active.is_(True), DrugCatalogEntry.name_lower.like("%" + like + "%", escape="\\"))
            .order_by(case((starts, 0), else_=1), DrugCatalogEntry.name_lower)
            .limit(MAX_RESULTS).all())


def get_drug(db: Session, drug_id: int) -> Optional[DrugCatalogEntry]:
    d = db.get(DrugCatalogEntry, drug_id)
    return d if d and d.is_active else None


def dosages_for(db: Session, drug: DrugCatalogEntry) -> list:
    return db.query(DrugDosage).filter(DrugDosage.drug_id == drug.id).order_by(DrugDosage.label).all()


def frequencies_for(db: Session, drug: DrugCatalogEntry, dosage: Optional[DrugDosage] = None) -> tuple:
    """(rows, preferred_group). The drug's own route group (plus the 'any' rows) first, everything else after, so a
    technician sees the relevant choices at the top without losing the rest. Unknown route: nothing is preferred and
    the whole list is returned in group order."""
    group = route_group(effective_route(drug, dosage))
    rows = db.query(DrugFrequency).filter(DrugFrequency.is_active.is_(True)).all()
    pref = {group, "any"} if group else set()
    rows.sort(key=lambda f: (0 if f.route_group in pref else 1, GROUP_ORDER.index(f.route_group), f.sort_order, f.label))
    return rows, group


def resolve_selection(db: Session, payload) -> dict:
    """Validate a guided-intake submission and return the values to store on a PatientMedication.

    `payload` needs: is_custom, name, eye, and either (drug_id, dosage_id, frequency_id) for a catalog pick or
    (dosage_text, frequency_text) for a custom entry. Server-side checks mirror the combobox chain, so a hand-built request
    can't record a dosage that belongs to a different drug or an eye that contradicts the route."""
    eye = (payload.eye or "").strip().upper()
    if eye and eye not in EYE_CHOICES:
        raise ValueError("Eye must be OU, OD, OS or PO.")

    if payload.is_custom:
        name = (payload.name or "").strip()
        if not name:
            raise ValueError("A medication name is required.")
        if not eye:
            raise ValueError("Choose OU, OD, OS or PO.")
        return {"name": name[:160], "strength": (payload.dosage_text or "").strip()[:80] or None,
                "frequency": (payload.frequency_text or "").strip()[:80] or None,
                "eye": eye, "route": "oral" if eye == "PO" else None,
                "drug_id": None, "dosage_id": None, "frequency_id": None, "drug_rxcui": None, "is_custom": True}

    drug = get_drug(db, payload.drug_id) if payload.drug_id else None
    if not drug:
        raise ValueError("Choose a medication from the list, or use Add Custom.")
    dosage = db.get(DrugDosage, payload.dosage_id) if payload.dosage_id else None
    if not dosage or dosage.drug_id != drug.id:
        raise ValueError("Choose a dosage that belongs to the selected medication.")
    freq = db.get(DrugFrequency, payload.frequency_id) if payload.frequency_id else None
    if not freq or not freq.is_active:
        raise ValueError("Choose a frequency.")

    group = route_group(effective_route(drug, dosage))
    if group == "oral":
        eye = "PO"                                       # an oral drug is always systemic, whatever was sent
    elif eye == "PO":
        raise ValueError("PO (by mouth) is only for oral medications.")
    elif not eye:
        raise ValueError("Choose OU, OD or OS.")
    return {"name": drug.name, "strength": dosage.label, "frequency": freq.label, "eye": eye,
            "route": effective_route(drug, dosage), "drug_id": drug.id, "dosage_id": dosage.id, "frequency_id": freq.id,
            "drug_rxcui": drug.rxcui, "is_custom": False}

"""Drug catalog lookups and the guided-intake validation (design: PATIENT_MEDICATION_LIST_DESIGN.md section 9).

Everything the medication intake combobox needs lives here, so routes stay thin and the UI never touches the database:

    search(db, q)                  -> Hit rows matching what the technician typed, by name or brand alias (min 3 characters, prefix matches first)
    dosages_for(db, drug)          -> that drug's dosages
    frequencies_for(db, drug)      -> the frequency pick list, the drug's own route group first
    resolve_selection(db, payload) -> validated, normalised values for a new patient medication, or ValueError(message)

Route handling: a route string is reduced to one of ophthalmic / oral / topical (or None when unknown) by `route_group`.
An ORAL drug always records eye = 'PO' (by mouth / systemic) and route 'oral'; 'PO' is refused for a drug whose route is
ophthalmic or topical, since those are applied to an eye or the skin. 'NA' (not applicable) is for drugs that are not given
to or for an eye (a skin cream, an injection, an inhaler...) and is accepted for any non-oral drug. A custom (free-text)
entry may use any eye value.
Nothing here sends anything outside the app: the catalog is rows in our own database.
"""
import re
from typing import Optional

from sqlalchemy.orm import Session

from ehr.models.drug_catalog import DrugAlias, DrugCatalogEntry, DrugDosage, DrugFrequency

MIN_QUERY_CHARS = 3
MAX_RESULTS = 15
EYE_CHOICES = ("OU", "OD", "OS", "PO", "NA")        # NA = not applicable (the drug is not given to or for an eye)
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


class Hit:
    """A search result: the catalog drug, and the brand name that matched when it was found through an alias."""
    def __init__(self, drug, via=None):
        self.drug, self.via = drug, via


def search(db: Session, q: str) -> list:
    """Active catalog drugs matching `q` by name or by brand alias (case-insensitive), as Hit rows: names starting with q,
    then aliases starting with q, then names containing q, then aliases containing q; alphabetical within each; max 15.
    A drug found by both its name and an alias is listed once, under whichever match ranks higher. A brand row that has been
    mapped to its generic is hidden in favour of the generic. Raises ValueError below the minimum length so
    a caller can't dump the catalog."""
    q = (q or "").strip().lower()
    if len(q) < MIN_QUERY_CHARS:
        raise ValueError(f"Type at least {MIN_QUERY_CHARS} characters to search.")
    like = "%" + _like_escape(q) + "%"
    ranked = {}                                                      # drug id -> (rank, name_lower, Hit)
    for d in (db.query(DrugCatalogEntry).filter(DrugCatalogEntry.is_active.is_(True), DrugCatalogEntry.name_lower.like(like, escape="\\"))
              .order_by(DrugCatalogEntry.name_lower).limit(60)):
        ranked[d.id] = (0 if d.name_lower.startswith(q) else 2, d.name_lower, Hit(d))
    for al, d in (db.query(DrugAlias, DrugCatalogEntry).join(DrugCatalogEntry, DrugCatalogEntry.id == DrugAlias.drug_id)
                  .filter(DrugCatalogEntry.is_active.is_(True), DrugAlias.alias_lower.like(like, escape="\\"))
                  .order_by(DrugAlias.alias_lower).limit(60)):
        rank = 1 if al.alias_lower.startswith(q) else 3
        if d.id not in ranked or rank < ranked[d.id][0] and ranked[d.id][0] != 0:
            ranked[d.id] = (rank, d.name_lower, Hit(d, via=al.alias))
    ordered = [h for _, _, h in sorted(ranked.values(), key=lambda t: (t[0], t[1]))]
    # A brand row whose generic has been looked up is replaced by that generic ("furosemide -- brand: Lasix"): hide the brand row
    # when an active generic carries its name as an alias. (A brand with no generic on record stays listed.)
    brands = {h.drug.name_lower for h in ordered if h.via is None and h.drug.tty == "BN" and h.drug.generic_checked_at is not None}
    if brands:
        redirected = {al for (al,) in db.query(DrugAlias.alias_lower).join(DrugCatalogEntry, DrugCatalogEntry.id == DrugAlias.drug_id)
                      .filter(DrugAlias.alias_lower.in_(brands), DrugCatalogEntry.is_active.is_(True)).all()}
        ordered = [h for h in ordered if not (h.via is None and h.drug.tty == "BN" and h.drug.name_lower in redirected)]
    return ordered[:MAX_RESULTS]


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
        raise ValueError("Eye must be OU, OD, OS, PO or NA.")

    if payload.is_custom:
        name = (payload.name or "").strip()
        if not name:
            raise ValueError("A medication name is required.")
        if not eye:
            raise ValueError("Choose OU, OD, OS, PO or NA.")
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
        raise ValueError("Choose OU, OD, OS or NA.")
    return {"name": drug.name, "strength": dosage.label, "frequency": freq.label, "eye": eye,
            "route": effective_route(drug, dosage), "drug_id": drug.id, "dosage_id": dosage.id, "frequency_id": freq.id,
            "drug_rxcui": drug.rxcui, "is_custom": False}

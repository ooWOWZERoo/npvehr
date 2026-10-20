"""NLM RxNorm (RxNav REST API) loader for the drug catalog (design: PATIENT_MEDICATION_LIST_DESIGN.md section 9).

Two stages, so the medication intake itself only ever reads our own database:

  1. load_display_names(db)  -- an administrator's action (or the command line): copies RxNorm's published list of drug
                                display names (~13,700: generic, brand and combination names, with tall-man lettering such as
                                "prednisoLONE") into drug_catalog as bare rows, source='rxnorm'. Re-running only adds names
                                that are new; nothing is ever deleted or overwritten.
  2. ensure_resolved(db, drug) -- the first time a technician picks one of those names, fetches that one drug's RxNorm id,
                                clinical-drug strengths and dose forms, stores them as drug_dosages, and derives the route from
                                the dose form ("Ophthalmic Solution" -> ophthalmic). A failed lookup never marks the drug
                                resolved, so it is simply tried again next time; the intake then offers manual entry.

What leaves the app: only the fixed RxNorm list request, and (per drug resolved) the picked drug NAME. Never patient data.
Which RxNav calls and response shapes this relies on were checked against the live service (apiVersion 3.1.356,
2026-10-20): Prescribe/displaynames.json, version.json, rxcui.json?name=&search=2, rxcui/{id}/properties.json,
rxcui/{id}/related.json?tty=SCD+SBD+DF. RXNAV_BASE_URL overrides the host (tests point it at a local stand-in).

Dosage rules (RxNorm term types): a brand name (BN) lists its branded clinical drugs; an ingredient (IN) or salt (PIN) lists
only its SINGLE-ingredient generic clinical drugs (combinations belong to the combination's own name, a MIN such as
"dorzolamide / timolol", which lists its own); anything else resolves with no dosages.
"""
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from typing import Optional

from sqlalchemy import insert
from sqlalchemy.orm import Session

from ehr.models.drug_catalog import DrugCatalogEntry, DrugCatalogLoadEvent, DrugDosage

log = logging.getLogger(__name__)
BASE_URL = os.environ.get("RXNAV_BASE_URL", "https://rxnav.nlm.nih.gov/REST").rstrip("/")
TIMEOUT_SECONDS = 8
MAX_NAME = 160
MAX_DOSAGES = 100
USER_AGENT = "NewPathVisionEHR/1.0 (drug catalog loader)"


class RxNavError(Exception):
    """RxNav could not be reached or answered with something unusable."""


def _get(path: str, params: Optional[dict] = None) -> dict:
    url = BASE_URL + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:      # incl. HTTPError, JSON decode errors
        raise RxNavError(f"{path}: {e}") from e


# ------------------------------------------------------------------------------------------ stage 1: names
def fetch_version() -> Optional[str]:
    try:
        return (_get("/version.json").get("version") or None)
    except RxNavError:
        return None


def fetch_display_names() -> list:
    try:
        names = _get("/Prescribe/displaynames.json")["displayTermsList"]["term"]
    except (KeyError, TypeError) as e:
        raise RxNavError(f"unexpected displaynames response: {e}") from e
    if not isinstance(names, list):
        raise RxNavError("unexpected displaynames response: term is not a list")
    return names


def usable_name(name) -> Optional[str]:
    """A display name worth offering: starts with a letter or digit (the list also holds bare chemical fragments such as
    "(1,1-dimethylethyl)urea") and fits the column. Original casing is kept, tall-man letters included."""
    n = (name or "").strip() if isinstance(name, str) else ""
    return n if n and len(n) <= MAX_NAME and re.match(r"[A-Za-z0-9]", n) else None


def load_display_names(db: Session, user_id: Optional[int] = None) -> DrugCatalogLoadEvent:
    """Add every RxNorm display name not already in the catalog. Always records an audit event (ok or failed); the caller
    commits. Existing rows (RxNorm or manual) are never touched, so administrators' own entries and any resolved drugs stay."""
    version = fetch_version()
    try:
        names = fetch_display_names()
    except RxNavError as e:
        log.warning("RxNorm name load failed: %s", e)
        ev = DrugCatalogLoadEvent(user_id=user_id, status="failed", rxnorm_version=version, error=str(e)[:255])
        db.add(ev)
        return ev
    have = {lower for (lower,) in db.query(DrugCatalogEntry.name_lower).all()}
    now = datetime.utcnow()
    rows = []
    for raw in names:
        n = usable_name(raw)
        if n and n.lower() not in have:
            have.add(n.lower())
            rows.append({"name": n, "name_lower": n.lower(), "source": "rxnorm", "is_active": True, "created_at": now})
    if rows:
        db.execute(insert(DrugCatalogEntry), rows)
    ev = DrugCatalogLoadEvent(user_id=user_id, status="ok", names_in_source=len(names), names_added=len(rows), rxnorm_version=version)
    db.add(ev)
    return ev


# ------------------------------------------------------------------------------------- stage 2: one drug
def _concepts(related: dict, tty: str) -> list:
    for g in related.get("relatedGroup", {}).get("conceptGroup", []) or []:
        if g.get("tty") == tty:
            return [c.get("name", "") for c in g.get("conceptProperties", []) or [] if c.get("name")]
    return []


def build_dosages(tty: str, rx_name: str, scd_names: list, sbd_names: list, dose_forms: list) -> list:
    """[(label, dose_form or None)] for one resolved drug, per the rules in the module docstring. Pure function."""
    if tty == "BN":
        source = [re.sub(r"\s*\[[^\]]*\]\s*$", "", n).strip() for n in sbd_names]
        strip = None
    elif tty in ("IN", "PIN"):
        source = [n for n in scd_names if " / " not in n]
        strip = rx_name
    elif tty == "MIN":
        source, strip = list(scd_names), None
    else:
        return []
    forms = sorted(dose_forms, key=len, reverse=True)                     # longest first: "Extended Release Oral Tablet" before "Oral Tablet"
    seen, out = set(), []
    for n in source:
        label = n
        if strip and label.lower().startswith(strip.lower() + " ") and re.match(r"\d", label[len(strip) + 1:]):
            label = label[len(strip) + 1:]       # "latanoprost 0.05 MG/ML ..." -> "0.05 MG/ML ..."; but keep "warfarin sodium 1 MG ..." whole
        label = label.strip()[:160]
        if not label or label.lower() in seen:
            continue
        seen.add(label.lower())
        out.append((label, next((f for f in forms if n.lower().endswith(f.lower())), None)))
    out.sort(key=lambda x: x[0].lower())
    return out[:MAX_DOSAGES]


def fetch_drug_detail(name: str) -> Optional[dict]:
    """Look one drug up by name. None when RxNorm does not know it; raises RxNavError when RxNav can't be reached."""
    ids = (_get("/rxcui.json", {"name": name, "search": 2}).get("idGroup") or {}).get("rxnormId") or []
    if not ids:
        return None
    rxcui = str(ids[0])
    props = _get(f"/rxcui/{rxcui}/properties.json").get("properties") or {}
    related = _get(f"/rxcui/{rxcui}/related.json?tty=SCD+SBD+DF")      # '+' is RxNav's tty-list separator, so it stays literal
    tty = props.get("tty") or ""
    dosages = build_dosages(tty, props.get("name") or name, _concepts(related, "SCD"), _concepts(related, "SBD"), _concepts(related, "DF"))
    return {"rxcui": rxcui, "tty": tty, "dosages": dosages}


def ensure_resolved(db: Session, drug: DrugCatalogEntry) -> str:
    """Resolve an RxNorm catalog row on first use. Returns 'local' (nothing to do: manual row or already resolved), 'ok',
    'not_found' (RxNorm has no entry; resolved with no dosages), or 'unavailable' (RxNav unreachable; left unresolved so the
    next pick retries). The caller commits."""
    if drug.source != "rxnorm" or drug.resolved_at is not None:
        return "local"
    try:
        detail = fetch_drug_detail(drug.name)
    except RxNavError as e:
        log.warning("RxNorm lookup for %r failed: %s", drug.name, e)
        return "unavailable"
    drug.resolved_at = datetime.utcnow()
    if detail is None:
        return "not_found"
    drug.tty = detail["tty"][:10] or None
    taken = db.query(DrugCatalogEntry.id).filter(DrugCatalogEntry.rxcui == detail["rxcui"], DrugCatalogEntry.id != drug.id).first()
    if not taken:                                                         # rxcui is unique; two names can map to one concept
        drug.rxcui = detail["rxcui"]
    have = {l.lower() for (l,) in db.query(DrugDosage.label).filter(DrugDosage.drug_id == drug.id).all()}
    for label, form in detail["dosages"]:
        if label.lower() not in have:
            db.add(DrugDosage(drug_id=drug.id, label=label, dose_form=(form or None), route=(form or None)))
    forms = Counter(f for _, f in detail["dosages"] if f)
    if forms:
        drug.default_route = sorted(forms.items(), key=lambda kv: (-kv[1], kv[0]))[0][0][:40]
    return "ok"


if __name__ == "__main__":                       # python -m ehr.services.rxnorm  -> load the RxNorm names from the command line
    from ehr.models.database import SessionLocal
    with SessionLocal() as _db:
        _ev = load_display_names(_db)
        _db.commit()
        print(f"{_ev.status}: {_ev.names_added} added of {_ev.names_in_source} in RxNorm ({_ev.rxnorm_version or 'version unknown'})"
              + (f" -- {_ev.error}" if _ev.error else ""))
        sys.exit(0 if _ev.status == "ok" else 1)

"""NLM RxNorm (RxNav REST API) loader for the drug catalog (design: PATIENT_MEDICATION_LIST_DESIGN.md section 9).

Two stages, so the medication intake itself only ever reads our own database:

  1. load_display_names(db)  -- an administrator's action (or the command line; scheduled_refresh repeats it monthly): copies RxNorm's published list of drug
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
rxcui/{id}/related.json?tty=SCD+SBD+DF+BN (and tty=IN+MIN for a brand's generics). RXNAV_BASE_URL overrides the host (tests
point it at a local stand-in).

Dosage rules (RxNorm term types): a brand name (BN) lists its branded clinical drugs; an ingredient (IN) or salt (PIN) lists
only its SINGLE-ingredient generic clinical drugs (combinations belong to the combination's own name, a MIN such as
"dorzolamide / timolol", which lists its own); anything else resolves with no dosages.

Brand names: resolving an ingredient also records its brands as aliases ("Coumadin" -> warfarin), so brands then find the
generic locally. A brand typed before that has happened (and absent from the name list) is looked up live once, by exact
name: RxNorm's generic for it is found or added to the catalog and the brand is cached as an alias. A brand of a combination
product maps to the combination ("dorzolamide / timolol"), not to each ingredient.
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

from sqlalchemy import insert, update
from sqlalchemy.orm import Session

from ehr.models.drug_catalog import DrugAlias, DrugCatalogEntry, DrugCatalogLoadEvent, DrugDosage
from ehr.services import drug_catalog as catalog
from ehr.services.drug_catalog import Hit

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


def fetch_brand_list() -> list:
    """RxNorm's brand names as [(name, rxcui)] (`allconcepts.json?tty=BN`, ~5,000)."""
    try:
        concepts = _get("/allconcepts.json", {"tty": "BN"})["minConceptGroup"]["minConcept"]
        return [(c["name"], str(c["rxcui"])) for c in concepts if c.get("name") and c.get("rxcui")]
    except (KeyError, TypeError) as e:
        raise RxNavError(f"unexpected brand list response: {e}") from e


def load_display_names(db: Session, user_id: Optional[int] = None) -> DrugCatalogLoadEvent:
    """Add every RxNorm display name and brand name not already in the catalog, and flag which RxNorm rows are brands
    (tty 'BN', with their RxNorm id) so a brand can later be mapped to its generic. Always records an audit event (ok or
    failed); the caller commits. Nothing is deleted or overwritten: manual rows and resolved drugs stay as they are, and an
    existing RxNorm row only gains its brand flag and id when it has neither. If only the brand list can't be fetched, the
    names still load and the event notes it."""
    version = fetch_version()
    try:
        names = fetch_display_names()
    except RxNavError as e:
        log.warning("RxNorm name load failed: %s", e)
        ev = DrugCatalogLoadEvent(user_id=user_id, status="failed", rxnorm_version=version, error=str(e)[:255])
        db.add(ev)
        return ev
    brand_error = None
    try:
        brands = fetch_brand_list()
    except RxNavError as e:
        log.warning("RxNorm brand list failed: %s", e)
        brands, brand_error = [], "Drug names loaded, but the brand list could not be fetched this time."
    existing = {lower: (id_, tty, rxcui, source) for id_, lower, tty, rxcui, source in
                db.query(DrugCatalogEntry.id, DrugCatalogEntry.name_lower, DrugCatalogEntry.tty, DrugCatalogEntry.rxcui, DrugCatalogEntry.source).all()}
    taken_rxcuis = {r for (_, _, r, _) in existing.values() if r}
    now = datetime.utcnow()
    rows, flag, added = [], [], {}                                 # added: name_lower -> index in rows, for names inserted below

    def new_row(name, tty=None, rxcui=None):
        return {"name": name, "name_lower": name.lower(), "source": "rxnorm", "is_active": True, "created_at": now, "tty": tty, "rxcui": rxcui}

    name_keys = set()
    for raw in names:
        n = usable_name(raw)
        if n:
            name_keys.add(n.lower())
            if n.lower() not in existing and n.lower() not in added:
                added[n.lower()] = len(rows)
                rows.append(new_row(n))
    brand_only = 0
    for raw, rxcui in brands:
        n = usable_name(raw)
        if not n:
            continue
        key = n.lower()
        brand_only += key not in name_keys
        if rxcui in taken_rxcuis:
            continue
        have = existing.get(key)                                   # (id, tty, rxcui, source)
        if key in added:                                           # a plain name added just above: make it a brand row
            rows[added[key]]["tty"], rows[added[key]]["rxcui"] = "BN", rxcui
            taken_rxcuis.add(rxcui)
        elif have is None:                                         # a brand RxNorm's name list doesn't carry (e.g. Coumadin)
            added[key] = len(rows)
            rows.append(new_row(n, "BN", rxcui))
            taken_rxcuis.add(rxcui)
        elif have[3] == "rxnorm" and have[1] is None and have[2] is None:   # an existing plain RxNorm row that is really a brand
            flag.append({"id": have[0], "tty": "BN", "rxcui": rxcui})
            taken_rxcuis.add(rxcui)
    if rows:
        db.execute(insert(DrugCatalogEntry), rows)
    if flag:
        db.execute(update(DrugCatalogEntry), flag)
    ev = DrugCatalogLoadEvent(user_id=user_id, status="ok", names_in_source=len(names) + brand_only, names_added=len(rows),
                              rxnorm_version=version, error=brand_error)
    db.add(ev)
    return ev


def scheduled_refresh(db: Session) -> Optional[DrugCatalogLoadEvent]:
    """The monthly refresh: add any names RxNorm has published since the last load. It only runs once an administrator has done
    the first load (an empty catalog is left for them to start on purpose) -- returns None when it skips. The event it records
    has no user (shown as "Scheduled or command line"). The caller commits."""
    if not db.query(DrugCatalogEntry.id).filter(DrugCatalogEntry.source == "rxnorm").first():
        return None
    return load_display_names(db, None)


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
    related = _get(f"/rxcui/{rxcui}/related.json?tty=SCD+SBD+DF+BN")      # '+' is RxNav's tty-list separator, so it stays literal
    tty = props.get("tty") or ""
    dosages = build_dosages(tty, props.get("name") or name, _concepts(related, "SCD"), _concepts(related, "SBD"), _concepts(related, "DF"))
    return {"rxcui": rxcui, "tty": tty, "dosages": dosages, "brands": [] if tty == "BN" else _concepts(related, "BN")}


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
    for brand in detail["brands"]:
        add_alias(db, drug, brand)
    forms = Counter(f for _, f in detail["dosages"] if f)
    if forms:
        drug.default_route = sorted(forms.items(), key=lambda kv: (-kv[1], kv[0]))[0][0][:40]
    return "ok"


def add_alias(db: Session, drug: DrugCatalogEntry, alias: str) -> bool:
    """Record `alias` as another name for `drug` unless it is the drug's own name or already recorded. Returns True if added."""
    a = (alias or "").strip()[:160]
    if not a or a.lower() == drug.name_lower:
        return False
    if db.query(DrugAlias.id).filter(DrugAlias.drug_id == drug.id, DrugAlias.alias_lower == a.lower()).first():
        return False
    db.add(DrugAlias(drug_id=drug.id, alias=a, alias_lower=a.lower(), source="rxnorm"))
    return True


def fetch_generics_for_rxcui(rxcui: str) -> list:
    """The generic(s) of a brand by RxNorm id: [(name, rxcui, tty)]. A combination brand maps to the combination (MIN), not each
    ingredient. Raises RxNavError when RxNav can't be reached."""
    related = _get(f"/rxcui/{rxcui}/related.json?tty=IN+MIN")
    found = {}
    for g in related.get("relatedGroup", {}).get("conceptGroup", []) or []:
        found[g.get("tty")] = [(c["name"], str(c["rxcui"]), g["tty"]) for c in g.get("conceptProperties", []) or [] if c.get("name")]
    return found.get("MIN") or found.get("IN") or []


def fetch_brand_generics(name: str) -> Optional[dict]:
    """Exact-name lookup of a brand: {"brand": name, "generics": [(name, rxcui, tty)]}. None when RxNorm doesn't know the
    name or it isn't a brand; raises RxNavError when RxNav can't be reached."""
    ids = (_get("/rxcui.json", {"name": name, "search": 2}).get("idGroup") or {}).get("rxnormId") or []
    if not ids:
        return None
    rxcui = str(ids[0])
    props = _get(f"/rxcui/{rxcui}/properties.json").get("properties") or {}
    if props.get("tty") != "BN":
        return None
    generics = fetch_generics_for_rxcui(rxcui)
    return {"brand": props.get("name") or name, "generics": generics} if generics else None


def _attach_generics(db: Session, brand: str, generics: list) -> list:
    """Find or add the generic catalog rows for a brand and record the brand as an alias of each; returns them as Hits."""
    hits = []
    for gname, grxcui, gtty in generics:
        drug = db.query(DrugCatalogEntry).filter(DrugCatalogEntry.name_lower == gname.lower()).first()
        if drug is None:
            taken = db.query(DrugCatalogEntry.id).filter(DrugCatalogEntry.rxcui == grxcui).first()
            drug = DrugCatalogEntry(name=gname[:MAX_NAME], name_lower=gname.lower()[:MAX_NAME], source="rxnorm", is_active=True,
                                    rxcui=None if taken else grxcui, tty=gtty)
            db.add(drug)
            db.flush()
        if drug.is_active:
            add_alias(db, drug, brand)
            hits.append(Hit(drug, via=brand))
    return hits


def brand_fallback(db: Session, query: str) -> list:
    """For a search the local catalog could not answer: treat the query as a brand name, ask RxNorm for its generic(s), find or
    add them to the catalog, cache the brand as an alias, and return them as Hits (via = the brand). Anything that goes wrong
    (not a brand, RxNav unreachable) is an empty result -- Add Custom is still there. The caller commits."""
    try:
        found = fetch_brand_generics(query.strip())
    except RxNavError as e:
        log.warning("RxNorm brand lookup for %r failed: %s", query, e)
        return []
    return _attach_generics(db, found["brand"], found["generics"]) if found else []


def map_brand(db: Session, drug: DrugCatalogEntry) -> str:
    """Look up the generic of a brand catalog row, once: records the brand as an alias of its generic(s) (which makes search
    show the generic, "furosemide -- brand: Lasix", instead of the brand row). Returns 'skipped' (not a brand, or already
    checked), 'ok', 'none' (RxNorm lists no generic; the brand row stays as it is) or 'unavailable' (RxNav unreachable; not
    marked checked, so it is tried again later). The caller commits."""
    if drug.tty != "BN" or drug.generic_checked_at is not None:
        return "skipped"
    generics = []
    if drug.rxcui:
        try:
            generics = fetch_generics_for_rxcui(drug.rxcui)
        except RxNavError as e:
            log.warning("RxNorm generic lookup for %r failed: %s", drug.name, e)
            return "unavailable"
    _attach_generics(db, drug.name, generics)
    drug.generic_checked_at = datetime.utcnow()
    return "ok" if generics else "none"


MAX_BRAND_LOOKUPS = 3        # per search: brand rows about to be shown that are mapped to their generic live (about 0.3 s each)


def search(db: Session, q: str) -> list:
    """The intake's search: the local catalog by name and brand alias, with each brand row that is about to be shown mapped to its
    generic first (so "lasix" offers furosemide, not the brand row), and, when nothing local matches at all, a live
    exact-name brand lookup. Network trouble never fails the search -- it just returns what is already local. The caller
    commits. Raises ValueError for a query under the minimum length (like catalog.search)."""
    hits = catalog.search(db, q)
    ql = q.strip().lower()
    pending = [h for h in hits if h.via is None and h.drug.tty == "BN" and h.drug.generic_checked_at is None and h.drug.name_lower.startswith(ql)]
    mapped = False
    for h in pending[:MAX_BRAND_LOOKUPS]:
        outcome = map_brand(db, h.drug)
        if outcome == "unavailable":
            break                                          # don't wait on a second timeout
        mapped = mapped or outcome == "ok"
    if mapped:
        db.flush()
        hits = catalog.search(db, q)
    return hits or brand_fallback(db, q)


if __name__ == "__main__":                       # python -m ehr.services.rxnorm  -> load the RxNorm names from the command line
    from ehr.models.database import SessionLocal
    with SessionLocal() as _db:
        _ev = load_display_names(_db)
        _db.commit()
        print(f"{_ev.status}: {_ev.names_added} added of {_ev.names_in_source} in RxNorm ({_ev.rxnorm_version or 'version unknown'})"
              + (f" -- {_ev.error}" if _ev.error else ""))
        sys.exit(0 if _ev.status == "ok" else 1)

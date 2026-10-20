"""Unit tests for the RxNorm loader (ehr/services/rxnorm.py): no browser, no network. The network layer is replaced by a replay
of REAL RxNav responses captured from the live service (tests/fixtures/rxnav_captured.json), keyed by the exact request."""
import json
import os
import tempfile
import urllib.parse
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ehr.models.database import Base
from ehr.models.drug_catalog import DrugCatalogEntry, DrugCatalogLoadEvent, DrugDosage
from ehr.services import rxnorm

CAPTURED = json.loads((Path(__file__).parent / "fixtures" / "rxnav_captured.json").read_text())["responses"]


@pytest.fixture
def db(monkeypatch):
    def replay(path, params=None):
        key = path + ("?" + urllib.parse.urlencode(params) if params else "")
        if key not in CAPTURED:
            raise rxnorm.RxNavError(f"not captured: {key}")
        return CAPTURED[key]
    monkeypatch.setattr(rxnorm, "_get", replay)
    with tempfile.TemporaryDirectory(prefix="npvehr-rxtest-") as tmp:
        engine = create_engine(f"sqlite:///{os.path.join(tmp, 't.db')}")
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine)() as session:
            yield session
        engine.dispose()


def _labels(db, name):
    d = db.query(DrugCatalogEntry).filter(DrugCatalogEntry.name_lower == name.lower()).one()
    return d, sorted(x.label for x in db.query(DrugDosage).filter(DrugDosage.drug_id == d.id).all())


def test_usable_name_skips_chemical_fragments_and_overlong_names_but_keeps_tall_man_lettering():
    assert rxnorm.usable_name("prednisoLONE") == "prednisoLONE"
    assert rxnorm.usable_name("  5-fluorouracil ") == "5-fluorouracil"
    assert rxnorm.usable_name("(1,1-dimethylethyl)urea") is None and rxnorm.usable_name("(-)-ambroxide") is None
    assert rxnorm.usable_name("x" * 161) is None and rxnorm.usable_name("") is None and rxnorm.usable_name(None) is None


def test_load_display_names_adds_only_usable_new_names_records_the_event_and_is_rerun_safe(db):
    db.add(DrugCatalogEntry(name="Latanoprost", name_lower="latanoprost", source="manual", is_active=True))   # an administrator's own row
    db.commit()
    ev = rxnorm.load_display_names(db, user_id=None)
    db.commit()
    assert ev.status == "ok" and ev.rxnorm_version == "05-Oct-2026" and ev.names_in_source == 12 and ev.names_added == 9   # 12 listed, 2 junk, 1 already there
    names = {d.name for d in db.query(DrugCatalogEntry).all()}
    assert "prednisoLONE" in names and "(1,1-dimethylethyl)urea" not in names
    mine = db.query(DrugCatalogEntry).filter(DrugCatalogEntry.name_lower == "latanoprost").one()
    assert (mine.name, mine.source) == ("Latanoprost", "manual")                      # untouched: not duplicated, not overwritten
    assert all(d.source == "rxnorm" and d.resolved_at is None and d.rxcui is None for d in db.query(DrugCatalogEntry).filter(DrugCatalogEntry.source == "rxnorm"))
    again = rxnorm.load_display_names(db)
    db.commit()
    assert again.status == "ok" and again.names_added == 0                              # nothing new the second time
    assert db.query(DrugCatalogLoadEvent).count() == 2


def test_failed_name_load_adds_nothing_and_records_a_failed_event(db, monkeypatch):
    def down(path, params=None):
        raise rxnorm.RxNavError("stand-in outage")
    monkeypatch.setattr(rxnorm, "_get", down)
    ev = rxnorm.load_display_names(db)
    db.commit()
    assert ev.status == "failed" and "outage" in ev.error and db.query(DrugCatalogEntry).count() == 0


def test_resolving_an_ingredient_lists_single_ingredient_strengths_and_derives_the_route(db):
    rxnorm.load_display_names(db)
    drug, labels = _labels(db, "latanoprost")
    assert rxnorm.ensure_resolved(db, drug) == "ok"
    db.commit()
    drug, labels = _labels(db, "latanoprost")
    assert drug.rxcui == "43611" and drug.tty == "IN" and drug.resolved_at is not None and drug.default_route.startswith("Ophthalmic")
    assert labels == ["0.05 MG/ML Ophthalmic Solution", "0.05 MG/ML Ophthalmic Suspension"]       # the latanoprost/netarsudil and /timolol combinations are NOT listed
    assert rxnorm.ensure_resolved(db, drug) == "local"                                            # already resolved: no second lookup


def test_resolving_keeps_the_full_name_when_the_product_name_differs_from_the_ingredient(db):
    rxnorm.load_display_names(db)
    drug, _ = _labels(db, "warfarin")
    assert rxnorm.ensure_resolved(db, drug) == "ok"
    db.commit()
    drug, labels = _labels(db, "warfarin")
    assert "warfarin sodium 1 MG Oral Tablet" in labels and not any(l.startswith("sodium ") for l in labels)   # never the misleading "sodium 1 MG ..."
    assert drug.default_route == "Oral Tablet"


def test_resolving_a_brand_and_a_combination_name(db):
    rxnorm.load_display_names(db)
    brand, _ = _labels(db, "xalatan")
    combo, _ = _labels(db, "dorzolamide / timolol")
    assert rxnorm.ensure_resolved(db, brand) == "ok" and rxnorm.ensure_resolved(db, combo) == "ok"
    db.commit()
    assert _labels(db, "xalatan")[1] == ["latanoprost 0.05 MG/ML Ophthalmic Solution"]            # brand: the branded product, "[Xalatan]" dropped
    assert all("dorzolamide" in l and "timolol" in l for l in _labels(db, "dorzolamide / timolol")[1])


def test_names_rxnorm_knows_but_has_no_dosages_for_resolve_empty_and_are_not_retried(db):
    rxnorm.load_display_names(db)
    for name in ("timolol maleate", "cajuput oil"):
        drug, _ = _labels(db, name)
        assert rxnorm.ensure_resolved(db, drug) == "ok"
        db.commit()
        drug, labels = _labels(db, name)
        assert labels == [] and drug.resolved_at is not None and drug.default_route is None


def test_a_name_rxnorm_does_not_know_is_marked_resolved_with_no_dosages(db):
    d = DrugCatalogEntry(name="zzzznotadrug", name_lower="zzzznotadrug", source="rxnorm", is_active=True)
    db.add(d)
    db.flush()
    assert rxnorm.ensure_resolved(db, d) == "not_found" and d.resolved_at is not None and d.rxcui is None


def test_an_unreachable_service_leaves_the_drug_unresolved_so_the_next_pick_retries(db, monkeypatch):
    rxnorm.load_display_names(db)
    drug, _ = _labels(db, "latanoprost")
    monkeypatch.setattr(rxnorm, "_get", lambda path, params=None: (_ for _ in ()).throw(rxnorm.RxNavError("down")))
    assert rxnorm.ensure_resolved(db, drug) == "unavailable" and drug.resolved_at is None
    assert db.query(DrugDosage).count() == 0


def test_two_names_that_map_to_one_rxnorm_concept_do_not_violate_the_unique_rxcui(db):
    a = DrugCatalogEntry(name="latanoprost", name_lower="latanoprost", source="rxnorm", is_active=True)
    b = DrugCatalogEntry(name="Latanoprost PF", name_lower="latanoprost pf", source="rxnorm", is_active=True)
    db.add_all([a, b])
    db.flush()
    # both names resolve (via the same captured concept) to rxcui 43611
    real = rxnorm._get
    rxnorm._get = lambda path, params=None: real("/rxcui.json", {"name": "latanoprost", "search": 2}) if path == "/rxcui.json" else real(path, params)
    try:
        assert rxnorm.ensure_resolved(db, a) == "ok" and rxnorm.ensure_resolved(db, b) == "ok"
        db.commit()
    finally:
        rxnorm._get = real
    assert a.rxcui == "43611" and b.rxcui is None                                                 # the second keeps NULL instead of failing
    assert db.query(DrugDosage).filter(DrugDosage.drug_id == b.id).count() == 2                    # but still gets its dosages

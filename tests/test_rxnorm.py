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
    # 16 names listed + 3 brands the list lacks = 19 in RxNorm; 2 junk fragments and the administrator's own "Latanoprost" are not added
    assert ev.status == "ok" and ev.rxnorm_version == "05-Oct-2026" and ev.names_in_source == 19 and ev.names_added == 16
    names = {d.name for d in db.query(DrugCatalogEntry).all()}
    assert "prednisoLONE" in names and "(1,1-dimethylethyl)urea" not in names
    mine = db.query(DrugCatalogEntry).filter(DrugCatalogEntry.name_lower == "latanoprost").one()
    assert (mine.name, mine.source) == ("Latanoprost", "manual")                      # untouched: not duplicated, not overwritten
    rx = db.query(DrugCatalogEntry).filter(DrugCatalogEntry.source == "rxnorm").all()
    assert all(d.resolved_at is None and d.generic_checked_at is None for d in rx)                  # nothing is looked up at load time
    assert all((d.rxcui is None) == (d.tty is None) for d in rx)                                    # only brand rows carry an id and a type yet
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


# ---------------------------------------------------------------------------------------------- brand names -> generics
from ehr.models.drug_catalog import DrugAlias  # noqa: E402
from ehr.services import drug_catalog as catalog  # noqa: E402


def test_resolving_an_ingredient_records_its_brands_and_a_brand_then_finds_the_generic(db):
    rxnorm.load_display_names(db)
    drug, _ = _labels(db, "warfarin")
    assert rxnorm.ensure_resolved(db, drug) == "ok"
    db.commit()
    assert sorted(a.alias for a in db.query(DrugAlias).filter(DrugAlias.drug_id == drug.id)) == ["Coumadin", "Jantoven"]
    hits = rxnorm.search(db, "COUMAD")                                                  # the Coumadin brand row is mapped to warfarin and hidden
    assert [(h.drug.name, h.via) for h in hits] == [("warfarin", "Coumadin")]
    assert rxnorm.ensure_resolved(db, drug) == "local"                                  # aliases are not re-fetched either


def test_a_brand_missing_from_the_name_list_is_looked_up_once_and_cached(db):
    assert catalog.search(db, "coumadin") == []                                          # empty catalog: nothing local
    hits = rxnorm.brand_fallback(db, "coumadin")
    db.commit()
    assert [(h.drug.name, h.via, h.drug.rxcui, h.drug.tty, h.drug.source) for h in hits] == [("warfarin", "Coumadin", "11289", "IN", "rxnorm")]
    again = catalog.search(db, "coumadin")                                               # cached: now answered locally
    assert [(h.drug.name, h.via) for h in again] == [("warfarin", "Coumadin")]
    assert db.query(DrugCatalogEntry).filter(DrugCatalogEntry.name_lower == "warfarin").count() == 1


def test_a_brand_fallback_reuses_an_existing_generic_row_instead_of_duplicating_it(db):
    rxnorm.load_display_names(db)
    db.commit()
    before = db.query(DrugCatalogEntry).count()
    hits = rxnorm.brand_fallback(db, "coumadin")
    db.commit()
    assert hits[0].drug.name == "warfarin" and db.query(DrugCatalogEntry).count() == before


def test_a_combination_brand_maps_to_the_combination_not_each_ingredient(db):
    hits = rxnorm.brand_fallback(db, "cosopt")
    assert [h.drug.name for h in hits] == ["dorzolamide / timolol"] and hits[0].via == "Cosopt"


def test_brand_fallback_returns_nothing_for_unknown_names_non_brands_and_outages(db, monkeypatch):
    assert rxnorm.brand_fallback(db, "zzzznotadrug") == []                              # RxNorm has no such name
    assert rxnorm.brand_fallback(db, "latanoprost") == []                               # known, but a generic, not a brand
    monkeypatch.setattr(rxnorm, "_get", lambda path, params=None: (_ for _ in ()).throw(rxnorm.RxNavError("down")))
    assert rxnorm.brand_fallback(db, "coumadin") == [] and db.query(DrugCatalogEntry).count() == 0


def test_search_ranks_name_prefixes_before_alias_prefixes_and_lists_a_drug_once(db):
    a = DrugCatalogEntry(name="warfarin", name_lower="warfarin", source="manual", is_active=True)
    b = DrugCatalogEntry(name="Waroxin", name_lower="waroxin", source="manual", is_active=True)
    c = DrugCatalogEntry(name="aspirin", name_lower="aspirin", source="manual", is_active=True)
    db.add_all([a, b, c])
    db.flush()
    db.add_all([DrugAlias(drug_id=a.id, alias="Warlock", alias_lower="warlock"), DrugAlias(drug_id=c.id, alias="Warrior", alias_lower="warrior"),
                DrugAlias(drug_id=a.id, alias="Warfarin Brand", alias_lower="warfarin brand")])
    db.commit()
    assert [(h.drug.name, h.via) for h in catalog.search(db, "war")] == [("warfarin", None), ("Waroxin", None), ("aspirin", "Warrior")]   # names first; warfarin once
    with pytest.raises(ValueError):
        catalog.search(db, "wa")
    assert rxnorm.add_alias(db, a, "WARFARIN") is False and rxnorm.add_alias(db, a, "Warlock") is False and rxnorm.add_alias(db, a, "Newname") is True


# ------------------------------------------------------------------------------------------------ scheduled refresh
def test_scheduled_refresh_waits_for_an_administrators_first_load_then_adds_only_what_is_new(db):
    assert rxnorm.scheduled_refresh(db) is None and db.query(DrugCatalogEntry).count() == 0       # nothing loaded yet: it does not start on its own
    rxnorm.load_display_names(db, user_id=None)
    db.commit()
    db.query(DrugCatalogEntry).filter(DrugCatalogEntry.name_lower == "lumigan").delete()           # as if RxNorm had published it since
    db.commit()
    ev = rxnorm.scheduled_refresh(db)
    db.commit()
    assert ev.status == "ok" and ev.names_added == 1 and ev.user_id is None
    assert db.query(DrugCatalogEntry).filter(DrugCatalogEntry.name_lower == "lumigan").count() == 1
    again = rxnorm.scheduled_refresh(db)
    assert again.names_added == 0


def test_a_failed_scheduled_refresh_is_recorded_and_changes_nothing(db, monkeypatch):
    rxnorm.load_display_names(db)
    db.commit()
    before = db.query(DrugCatalogEntry).count()
    monkeypatch.setattr(rxnorm, "_get", lambda path, params=None: (_ for _ in ()).throw(rxnorm.RxNavError("down")))
    ev = rxnorm.scheduled_refresh(db)
    db.commit()
    assert ev.status == "failed" and db.query(DrugCatalogEntry).count() == before


# ---------------------------------------------------------------------------------- brand rows in the list -> generics
def _hits(hits):
    return [(h.drug.name, h.via) for h in hits]


def _offline(monkeypatch):
    monkeypatch.setattr(rxnorm, "_get", lambda path, params=None: (_ for _ in ()).throw(rxnorm.RxNavError("offline")))


def test_the_load_flags_brand_rows_and_adds_brands_the_name_list_lacks_and_is_rerun_safe(db):
    rxnorm.load_display_names(db)
    db.commit()
    lasix = db.query(DrugCatalogEntry).filter(DrugCatalogEntry.name_lower == "lasix").one()
    coumadin = db.query(DrugCatalogEntry).filter(DrugCatalogEntry.name_lower == "coumadin").one()      # not in RxNorm's name list
    assert (lasix.tty, lasix.rxcui, coumadin.tty, coumadin.rxcui, coumadin.source) == ("BN", "202991", "BN", "202421", "rxnorm")
    assert db.query(DrugCatalogEntry).filter(DrugCatalogEntry.name_lower == "furosemide").one().tty is None   # generics stay unflagged
    assert rxnorm.load_display_names(db).names_added == 0


def test_a_later_load_flags_rows_an_earlier_load_added_before_brands_were_known(db, monkeypatch):
    real = rxnorm.fetch_brand_list
    monkeypatch.setattr(rxnorm, "fetch_brand_list", lambda: (_ for _ in ()).throw(rxnorm.RxNavError("no brand list")))
    first = rxnorm.load_display_names(db)
    db.commit()
    assert first.status == "ok" and "brand list" in first.error                          # names still loaded, the gap is noted
    assert db.query(DrugCatalogEntry).filter(DrugCatalogEntry.tty == "BN").count() == 0
    monkeypatch.setattr(rxnorm, "fetch_brand_list", real)
    again = rxnorm.load_display_names(db)
    db.commit()
    assert again.error is None and again.names_added == 3                              # the three brands the name list lacks
    assert db.query(DrugCatalogEntry).filter(DrugCatalogEntry.tty == "BN").count() == 7   # ...and the other four existing rows are now flagged


def test_a_brand_row_in_the_list_is_replaced_by_its_generic_and_the_mapping_is_remembered(db, monkeypatch):
    rxnorm.load_display_names(db)
    db.commit()
    assert _hits(catalog.search(db, "lasix")) == [("lasix", None)]                      # before mapping: just the brand row
    assert _hits(rxnorm.search(db, "lasix")) == [("furosemide", "lasix")]               # mapped now; the brand row is hidden
    db.commit()
    _offline(monkeypatch)
    assert _hits(rxnorm.search(db, "lasix")) == [("furosemide", "lasix")]               # remembered: no network needed
    assert _hits(rxnorm.search(db, "furosemide")) == [("furosemide", None)]
    assert _hits(rxnorm.search(db, "tylenol")) == [("tylenol", None)]                   # never mapped, and offline: the brand row is still listed


def test_a_partly_typed_brand_missing_from_the_name_list_finds_its_generic(db):
    rxnorm.load_display_names(db)
    db.commit()
    assert _hits(rxnorm.search(db, "coum")) == [("warfarin", "Coumadin")]                # "coum" -> Coumadin brand row -> warfarin
    assert _hits(rxnorm.search(db, "tylen")) == [("acetaminophen", "tylenol")]


def test_a_combination_brand_in_the_list_maps_to_the_combination(db):
    rxnorm.load_display_names(db)
    db.commit()
    assert _hits(rxnorm.search(db, "cosopt")) == [("dorzolamide / timolol", "Cosopt")]


def test_when_the_service_is_down_a_brand_row_stays_listed_and_is_mapped_next_time(db, monkeypatch):
    rxnorm.load_display_names(db)
    db.commit()
    real = rxnorm._get
    _offline(monkeypatch)
    assert _hits(rxnorm.search(db, "lasix")) == [("lasix", None)]                       # an outage never hides or fails the search
    assert db.query(DrugCatalogEntry).filter(DrugCatalogEntry.name_lower == "lasix").one().generic_checked_at is None
    monkeypatch.setattr(rxnorm, "_get", real)
    assert _hits(rxnorm.search(db, "lasix")) == [("furosemide", "lasix")]


def test_a_brand_with_no_generic_on_record_stays_listed_and_is_not_rechecked(db, monkeypatch):
    rxnorm.load_display_names(db)
    db.commit()
    monkeypatch.setattr(rxnorm, "fetch_generics_for_rxcui", lambda rxcui: [])
    drug = db.query(DrugCatalogEntry).filter(DrugCatalogEntry.name_lower == "lasix").one()
    assert rxnorm.map_brand(db, drug) == "none" and drug.generic_checked_at is not None
    assert rxnorm.map_brand(db, drug) == "skipped"
    assert _hits(catalog.search(db, "lasix")) == [("lasix", None)]


def test_a_search_maps_at_most_a_few_brand_rows_so_it_stays_quick(db, monkeypatch):
    for i in range(5):
        db.add(DrugCatalogEntry(name=f"Zzbrand{i}", name_lower=f"zzbrand{i}", source="rxnorm", is_active=True, tty="BN", rxcui=f"99000{i}"))
    db.commit()
    calls = []
    monkeypatch.setattr(rxnorm, "fetch_generics_for_rxcui", lambda rxcui: calls.append(rxcui) or [])
    rxnorm.search(db, "zzbrand")
    assert len(calls) == rxnorm.MAX_BRAND_LOOKUPS
    rxnorm.search(db, "zzbrand")                                                        # the next search maps the next few
    assert len(calls) == 5                                                              # all five brands now checked, none twice


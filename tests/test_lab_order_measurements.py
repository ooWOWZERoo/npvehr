"""Unit tests for the eyeglasses lab-order measurement rules (ehr.services.rx_lab_orders.clean_glasses_measurements): PD is always required,
seg height only for lens designs other than Single Vision. No browser."""
import pytest

from ehr.services import rx_lab_orders as lo


def clean(design="Single Vision", pd=None, r=None, l=None, near=None, sr=None, sl=None):
    return lo.clean_glasses_measurements(design, pd, r, l, near, sr, sl)


def test_single_vision_needs_only_a_pd_and_discards_any_seg_height():
    got = clean(pd="63", sr="18", sl="18")
    assert got == {"lens_design": "Single Vision", "pd_distance": 63.0, "pd_od": None, "pd_os": None, "pd_near": None,
                   "seg_height_od": None, "seg_height_os": None}                       # a single vision lens has no segment: nothing is kept


def test_pd_is_always_required_as_a_distance_pd_or_both_monocular_values():
    for kwargs in ({}, {"r": "31.5"}, {"l": "31.5"}, {"near": "60"}):
        with pytest.raises(lo.MeasurementError) as e:
            clean(**kwargs)
        assert e.value.errors == ["PD is required: enter the distance PD, or both the right and left PD."]
    both = clean(r="31.5", l="32")
    assert (both["pd_od"], both["pd_os"], both["pd_distance"]) == (31.5, 32.0, 63.5)    # total derived from the two when not given
    assert clean(pd="64", r="31.5", l="32")["pd_distance"] == 64.0                       # a given total is kept as entered
    assert clean(pd="63", near="60")["pd_near"] == 60.0


@pytest.mark.parametrize("design", ["Bifocal", "Trifocal", "Progressive", "Office-Computer"])
def test_seg_height_is_required_for_every_design_except_single_vision(design):
    with pytest.raises(lo.MeasurementError) as e:
        clean(design, pd="63")
    assert e.value.errors == ["Seg height is required for this lens design: enter the right and left seg height."]
    with pytest.raises(lo.MeasurementError):
        clean(design, pd="63", sr="18")                                                  # both eyes are needed
    got = clean(design, pd="63", sr="18", sl="17.5")
    assert (got["seg_height_od"], got["seg_height_os"], got["lens_design"]) == (18.0, 17.5, design)
    assert lo.seg_height_required(design) and not lo.seg_height_required("Single Vision") and not lo.seg_height_required(None)


def test_every_problem_is_reported_at_once_and_a_lens_design_is_required():
    with pytest.raises(lo.MeasurementError) as e:
        clean("Progressive")
    assert e.value.errors == ["PD is required: enter the distance PD, or both the right and left PD.",
                              "Seg height is required for this lens design: enter the right and left seg height."]
    for bad in ("", None, "Round", "single vision"):
        with pytest.raises(lo.MeasurementError) as e:
            clean(bad, pd="63")
        assert e.value.errors == ["Choose the lens design."]                             # without a design we can't tell whether seg height is needed


def test_typo_guards_catch_a_misplaced_decimal_or_non_numbers():
    with pytest.raises(lo.MeasurementError) as e:
        clean(pd="6.3")
    assert e.value.errors == ["Distance PD of 6.3 mm looks wrong (expected 40-85 mm)."]   # no second "PD is required" for a PD that was typed
    with pytest.raises(lo.MeasurementError) as e:
        clean(pd="63", near="abc")
    assert e.value.errors == ["Near PD must be a number in millimetres."]
    with pytest.raises(lo.MeasurementError) as e:
        clean("Progressive", pd="63", sr="180", sl="18")
    assert e.value.errors == ["Right seg height of 180 mm looks wrong (expected 5-40 mm)."]
    assert lo.NO_MEASUREMENTS == {k: None for k in lo.MEASURE_FIELDS}

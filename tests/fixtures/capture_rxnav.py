"""Re-captures tests/fixtures/rxnav_captured.json from the LIVE RxNav service (run by hand when the loader's calls change):

    python tests/fixtures/capture_rxnav.py

It runs the loader's own functions with the network layer wrapped to record each real response under the exact request
the loader made, so the test stand-in (tests/conftest.py) answers precisely what RxNav answered. The display-name list is
trimmed to the names below plus a few junk entries the loader must skip."""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ehr.services import rxnorm  # noqa: E402

NAMES = ["latanoprost", "xalatan", "dorzolamide / timolol", "warfarin", "timolol maleate", "cajuput oil", "zzzznotadrug"]
BRANDS = ["coumadin", "cosopt", "zzzznotadrug"]      # exact-name brand lookups (coumadin is NOT in the display-name list)
KEEP_IN_LIST = ["latanoprost", "xalatan", "dorzolamide / timolol", "warfarin", "timolol maleate", "cajuput oil", "prednisoLONE", "prednisoLONE acetate",
                "metFORMIN", "lumigan", "(1,1-dimethylethyl)urea", "(-)-ambroxide"]
recorded = {}
real_get = rxnorm._get


def recording_get(path, params=None):
    import urllib.parse
    key = path + ("?" + urllib.parse.urlencode(params) if params else "")
    for attempt in range(4):                       # the public service occasionally drops a connection
        try:
            recorded[key] = real_get(path, params)
            return recorded[key]
        except rxnorm.RxNavError:
            if attempt == 3:
                raise


rxnorm._get = recording_get
version = rxnorm.fetch_version()
names = rxnorm.fetch_display_names()
recorded["/Prescribe/displaynames.json"] = {"displayTermsList": {"term": [n for n in names if n in KEEP_IN_LIST]}}
for n in NAMES:
    rxnorm.fetch_drug_detail(n)
for brand in BRANDS:
    rxnorm.fetch_brand_generics(brand)
out = {"captured_from": "https://rxnav.nlm.nih.gov/REST", "captured_on": date.today().isoformat(), "rxnorm_version": version,
       "note": "Real RxNav responses, recorded by tests/fixtures/capture_rxnav.py. Only the displaynames list is trimmed.", "responses": recorded}
Path(__file__).with_name("rxnav_captured.json").write_text(json.dumps(out, indent=1, sort_keys=True))
print("captured", len(recorded), "responses; RxNorm version", version)

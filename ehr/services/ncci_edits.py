"""NCCI Procedure-to-Procedure (PTP) edit advisory check -- a narrow,
curated lookup, same "hardcoded table, not a live terminology/rules
service" posture already used for ICD-10 (ehr.services.ap_composer) and the
CPT catalog (ehr.services.cpt_mapper). Covers only the ophthalmology
testing-code pairs this app can actually produce today (see migration
049_seed_glaucoma_oct_test and migration_035_seed_cpt_codes), not a
general-purpose NCCI engine.

Sourced from the real CMS NCCI Procedure-to-Procedure (PTP) Edits,
Practitioner, version 32.3 (effective 2025; "CPT only copyright 2025
American Medical Association. All rights reserved.") -- the actual
quarterly file, not a third-party sample/demo dataset. Verified directly
against the file's Column 1/Column 2/Modifier Indicator/PTP Edit Rationale
fields for every pair below; confirmed no edit exists for 92083/92133 by
checking both column orderings across the full practitioner file set.

Advisory/decision-support only: this never blocks saving an appointment or
exam, never alters billing, and nothing here is transmitted as a real
claim -- the same posture as cpt_mapper's billing preview. This is an
intentional, narrow start of the billing/claims domain (PTP-edit advisories
only) -- see BUILD_BACKLOG.md and the baseline spec's Capability Boundary
note for what remains explicitly out of scope (EDI 837, a clearinghouse
relationship, MUE checks, claims submission).
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class NcciPair:
    column1: str
    column2: str
    modifier_indicator: int  # 0 = not allowed (mutually exclusive); 1 = modifier allowed
    rationale: str

    @property
    def message(self) -> str:
        if self.modifier_indicator == 0:
            return (f"NCCI edit: {self.column1} and {self.column2} cannot be billed together "
                     f"on the same date of service -- no modifier unbundles this pair ({self.rationale}).")
        return (f"NCCI edit: {self.column1} and {self.column2} are bundled by default -- "
                 f"modifier 25/59/XE/XS/XP/XU may unbundle them only with documented clinical "
                 f"justification for both on the same date ({self.rationale}).")


# CMS NCCI PTP Edits, Practitioner, v32.3 (effective 2025). Column 1/Column 2
# order preserved from the source file; check_pair/check_codes below are
# order-independent.
PTP_EDITS = [
    NcciPair("92133", "92134", 0, "CPT Manual or CMS manual coding instruction"),
    NcciPair("92133", "92250", 1, "Mutually exclusive procedures"),
    NcciPair("92134", "92250", 1, "Mutually exclusive procedures"),
]

_BY_PAIR = {}
for _edit in PTP_EDITS:
    _BY_PAIR[(_edit.column1, _edit.column2)] = _edit
    _BY_PAIR[(_edit.column2, _edit.column1)] = _edit


def check_pair(code1: str, code2: str):
    """Returns the NcciPair edit between these two CPT codes (order-independent),
    or None if no PTP edit exists in this curated table for that pair -- meaning
    either no edit exists at all, or (for any pair outside this narrow
    ophthalmology-testing scope) it simply hasn't been curated here yet."""
    if not code1 or not code2 or code1 == code2:
        return None
    return _BY_PAIR.get((code1, code2))


def check_codes(codes):
    """Given every CPT code appearing on one visit, returns every pairwise PTP
    edit found among them (order-independent, each pair reported once)."""
    unique_codes = sorted({c for c in codes if c})
    warnings = []
    for i, code1 in enumerate(unique_codes):
        for code2 in unique_codes[i + 1:]:
            edit = check_pair(code1, code2)
            if edit:
                warnings.append(edit)
    return warnings

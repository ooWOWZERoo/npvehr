"""Diabetic-retinopathy communication with the managing physician (staff-facing decision support; the reference is
MIPS Quality #019 / CMS142 as the practice's compliance analyst described it -- the practice's compliance expert owns
that interpretation, this module only encodes it).

When a letter is DUE (advisory banner on the exam page, never blocking):
    the exam's diagnosis codes (or an active Problem List entry) include a diabetic-retinopathy ICD-10 code
    (the E10 / E11 / E13 .31x-.35x family), AND the patient is 18 or older,
    AND no letter has been marked SENT, and no exclusion documented, for this patient in the last 12 months.
A letter is only valid with BOTH discrete facts the measure needs: the retinopathy SEVERITY and whether MACULAR EDEMA
is present or absent, plus the clinician confirming a dilated macular/fundus exam was done. Severity and edema are
suggested from the ICD-10 code (a clinician confirms or changes them); where the code doesn't say, nothing is guessed.
An exam recorded as NOT dilated never prompts (exams from before the dilation field existed, and any left blank, still do and
the clinician confirms on the letter form). The visit's CPT code is not checked (over-inclusive banner, harmless). Nothing is transmitted by this app: 'sent' is a
staff attestation of how it left the office.
"""
import re
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ehr.models.care_coordination import CommunicationTemplate, OutsidePractitioner, PatientPrimaryCare, PcpCommunication
from ehr.models.database import Problem

TEMPLATE_CODE = "pcp_diabetic_retinopathy"
TOKENS = ("pcp_name", "patient_name", "dob", "exam_date", "severity", "macular_edema", "findings", "plan", "provider_name")
REQUIRED_TOKENS = ("severity", "macular_edema")
SEVERITIES = ("mild", "moderate", "severe", "proliferative")
SEVERITY_LABEL = {"mild": "Mild non-proliferative", "moderate": "Moderate non-proliferative",
                  "severe": "Severe non-proliferative", "proliferative": "Proliferative"}
EDEMA_LABEL = {"present": "Present", "absent": "Absent"}
SEND_METHODS = {"fax": "Fax", "mail": "Mail", "direct": "Direct secure message", "phone": "Phone call", "handed": "Handed to patient"}
EXCLUSIONS = {"patient_refusal": "Patient refusal", "medical_contraindication": "Medical contraindication", "other": "Other (explain)"}
WINDOW_DAYS = 365
# Diabetes-with-ophthalmic-complication retinopathy family: E10, E11, E13 .31x-.35x (.37 = macular edema resolved, not a retinopathy stage).
_CODE_RE = re.compile(r"\bE(?:10|11|13)\.3([1-5])([0-9])\d?\b", re.I)
_SEVERITY_BY_DIGIT = {"2": "mild", "3": "moderate", "4": "severe", "5": "proliferative"}


def retinopathy_codes(text: Optional[str]) -> list:
    """[(code, severity|None, edema|None)] for each retinopathy code in free text. Severity comes from the 2nd digit
    after the decimal (1 = unspecified retinopathy), edema only from an explicit 'with' (1) / 'without' (9) final digit."""
    out = []
    for m in _CODE_RE.finditer(text or ""):
        sev = _SEVERITY_BY_DIGIT.get(m.group(1))
        edema = {"1": "present", "9": "absent"}.get(m.group(2))
        code = m.group(0).upper()
        if code not in [c for c, _, _ in out]:
            out.append((code, sev, edema))
    return out


def _age(dob: Optional[str], on: date) -> Optional[int]:
    try:
        d = date.fromisoformat((dob or "")[:10])
    except ValueError:
        return None
    return on.year - d.year - ((on.month, on.day) < (d.month, d.day))


def suggestion(db: Session, exam) -> dict:
    """What the form should prefill: severity and edema from the exam's codes (most severe wins), else from an active
    retinopathy Problem; 'ambiguous' stays None for the clinician to decide."""
    found = retinopathy_codes(getattr(exam, "diagnosis_codes", None))
    if not found:
        for p in db.query(Problem).filter(Problem.patient_id == exam.patient_id, Problem.status == "Active").all():
            found += retinopathy_codes(p.icd10_code)
    order = {s: i for i, s in enumerate(SEVERITIES)}
    sev = max((s for _, s, _ in found if s), key=lambda s: order[s], default=None)
    edemas = {e for _, _, e in found if e}
    return {"codes": [c for c, _, _ in found], "severity": sev, "edema": ("present" if "present" in edemas else ("absent" if edemas else None))}


def recent_status(db: Session, patient_id: int, now: Optional[datetime] = None) -> Optional[PcpCommunication]:
    """The newest SENT or EXCLUDED communication inside the 12-month window (what satisfies the measure), if any."""
    cutoff = (now or datetime.utcnow()) - timedelta(days=WINDOW_DAYS)
    return (db.query(PcpCommunication).filter(PcpCommunication.patient_id == patient_id, PcpCommunication.status.in_(("sent", "excluded")),
                                              func.coalesce(PcpCommunication.sent_at, PcpCommunication.created_at) >= cutoff)
            .order_by(PcpCommunication.id.desc()).first())


def candidate(db: Session, exam, now: Optional[datetime] = None) -> Optional[dict]:
    """None when no letter is due for this exam, else {'codes', 'severity', 'edema', 'draft'} (draft = an unsent letter
    already started for this exam). Never raises into a page."""
    try:
        if getattr(exam, "dilated_exam_performed", None) is False:
            return None                                   # the measure needs a dilated exam; this one was recorded as not dilated
        s = suggestion(db, exam)
        if not s["codes"]:
            return None
        age = _age(getattr(exam.patient, "date_of_birth", None), (now or datetime.utcnow()).date())
        if age is not None and age < 18:
            return None
        if recent_status(db, exam.patient_id, now):
            return None
        draft = (db.query(PcpCommunication).filter(PcpCommunication.exam_id == exam.id, PcpCommunication.status == "drafted")
                 .order_by(PcpCommunication.id.desc()).first())
        return {**s, "draft": draft}
    except Exception:                                  # noqa: BLE001 -- decision support must not break the page
        return None


# --------------------------------------------------------------------------------------- template / letter
def get_template(db: Session) -> Optional[CommunicationTemplate]:
    return db.query(CommunicationTemplate).filter(CommunicationTemplate.code == TEMPLATE_CODE).first()


def template_error(body: str) -> Optional[str]:
    """None if a template body is acceptable: only known {placeholders}, and the two facts the measure needs are present."""
    found = set(re.findall(r"\{([a-z_]+)\}", body or ""))
    unknown = sorted(found - set(TOKENS))
    if unknown:
        return "Unknown placeholder(s): " + ", ".join("{" + u + "}" for u in unknown) + ". Allowed: " + ", ".join("{" + t + "}" for t in TOKENS) + "."
    missing = [t for t in REQUIRED_TOKENS if t not in found]
    if missing:
        return "The letter must include " + " and ".join("{" + t + "}" for t in missing) + ": the severity of the retinopathy and whether macular edema is present are what the letter has to communicate."
    return None


def render(body: str, values: dict) -> str:
    """Whitelisted {placeholder} substitution only (no template engine, so nothing a user types can execute)."""
    return re.sub(r"\{([a-z_]+)\}", lambda m: str(values.get(m.group(1), m.group(0))) if m.group(1) in TOKENS else m.group(0), body or "")


def practitioner_label(p: Optional[OutsidePractitioner]) -> str:
    if not p:
        return ""
    name = f"{p.first_name} {p.last_name}" + (f", {p.credentials}" if p.credentials else "")
    return name


def recipient_snapshot(p: Optional[OutsidePractitioner]) -> Optional[str]:
    if not p:
        return None
    bits = [practitioner_label(p), p.practice_name, f"fax {p.fax}" if p.fax else None]
    return " | ".join(b for b in bits if b)[:400]


def primary_care_for(db: Session, patient_id: int) -> Optional[OutsidePractitioner]:
    row = db.query(PatientPrimaryCare).filter(PatientPrimaryCare.patient_id == patient_id).first()
    return row.practitioner if row and row.practitioner.is_active else None


# --------------------------------------------------------------------------------------- directory validation
def clean_fax(v: Optional[str]) -> Optional[str]:
    digits = re.sub(r"\D", "", v or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits or None


def valid_fax(digits: Optional[str]) -> bool:
    return digits is None or len(digits) == 10


def valid_npi(v: Optional[str]) -> bool:
    return not v or bool(re.fullmatch(r"\d{10}", v))

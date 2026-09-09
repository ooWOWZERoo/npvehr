"""Small template-context helpers shared across routes."""
from datetime import date

# Heuristic used for the "Allergies" flag on the patient context strip. A false
# negative here (missing a real allergy) is considered safer than a false
# positive (crying wolf / alert fatigue on every patient with a benign note),
# per the spec for this feature -- this is deliberately conservative, not a
# clinical-grade parser.
_NEGATIVE_ALLERGY_PHRASES = (
    "none", "no known", "nkda", "n/a", "na", "denies",
    "no allergies", "no known allergies", "no known drug allergies",
)


def compute_age(dob_str):
    """Best-effort age from a 'YYYY-MM-DD' date_of_birth string. Returns None
    (never an error) if the value is missing or unparsable."""
    if not dob_str:
        return None
    try:
        y, m, d = [int(part) for part in str(dob_str).split("-")[:3]]
        born = date(y, m, d)
    except (ValueError, TypeError):
        return None
    today = date.today()
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return age if age >= 0 else None


def has_notable_allergies(allergies_text) -> bool:
    """True only when `allergies_text` looks like it actually names an allergy,
    i.e. it is non-empty and doesn't match a common "none/NKDA"-style phrase."""
    if not allergies_text:
        return False
    text = str(allergies_text).strip().lower()
    if not text:
        return False
    for phrase in _NEGATIVE_ALLERGY_PHRASES:
        if phrase in text:
            return False
    return True


def display_name(patient) -> str:
    """'Last, First' normally, or 'Last, First \"Preferred\"' when a preferred_name
    is on file -- used everywhere the patient's full name is shown prominently
    (patient list, patient detail/workspace header, context strip)."""
    if patient is None:
        return ""
    base = f"{patient.last_name}, {patient.first_name}"
    preferred = (getattr(patient, "preferred_name", None) or "").strip()
    if preferred:
        return f'{base} "{preferred}"'
    return base


def patient_context(patient):
    """Build the dict base.html's patient-context-strip expects, or None."""
    if patient is None:
        return None
    return {
        "id": patient.id,
        "first_name": patient.first_name,
        "last_name": patient.last_name,
        "preferred_name": patient.preferred_name,
        "mrn": patient.mrn,
        "name": display_name(patient),
        "age": compute_age(patient.date_of_birth),
        "gender": patient.gender,
        "dob": patient.date_of_birth,
        "phone": patient.phone,
        "photo_path": patient.photo_path,
        "has_allergy_flag": has_notable_allergies(patient.allergies),
        "allergies_text": patient.allergies,
        "balance_due": patient.balance_due,
        "balance_state": balance_state(patient.balance_due),
    }


def balance_state(balance_due):
    """Classify a manually-entered balance_due value into one of three display
    states for the patient-context-strip balance box. None or 0 (within a cent)
    is treated as 'even' -- there is still no billing/ledger module behind this
    value (see Patient.balance_due), so this is a hand-maintained snapshot, not
    a computed one."""
    if balance_due is None:
        return "even"
    if balance_due > 0.005:
        return "due"
    if balance_due < -0.005:
        return "credit"
    return "even"

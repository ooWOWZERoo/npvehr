"""Bulk import of ROS prompts and their ICD-10/CPT rules from a spreadsheet (.csv or .xls) into the ROS catalog
(ehr/models/ros.py). Same stage -> preview -> confirm -> optional undo shape as the recall import, reusing its
file reader and the data_import_batches table; this is catalog content, not patient data.

One row per rule. Columns are found by header name (spelling variants accepted, order irrelevant):
  Body System*   one of the 13 systems in ROS_SYSTEMS (case-insensitive)
  Prompt*        the ROS prompt, up to 255 characters
  ICD-10         suggested diagnosis code, e.g. E11.9            } ICD-10 and CPT go together: a row with one but
  ICD-10 Family  optional supporting prefix, e.g. E11 or E11.3   } not the other is skipped. A row with neither
  CPT            recommended test code, e.g. 92250               } just adds the prompt.
  Note           the compliance note shown with the rule
  Source         where the rule came from; kept inside the note as "Source (not yet verified): ..."
Everything imported lands UNREVIEWED: a rule is never marked reviewed, and no citation is recorded, by an import.
Sign-off stays a deliberate act in /admin/ros (reviewer + time recorded). Existing prompts and rules are matched
and left exactly as they are -- the import only adds -- so re-running a file adds nothing the second time.
"""
import csv
import io
import re
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ehr.models.database import CptCode
from ehr.models.imports import DataImportBatch, DataImportBatchRecord
from ehr.models.ros import ROS_SYSTEMS, EncounterRosResponse, RosIcd10TestMapping, RosMaster
from ehr.services import field_audit
from ehr.services.recall_import import ImportFileError, _raw_grid

KIND = "ros_catalog"
TEMPLATE_COLUMNS = ["Body System", "Prompt", "ICD-10", "ICD-10 Family", "CPT", "Note", "Source"]
_HEADERS = {"bodysystem": "system", "system": "system", "prompt": "prompt", "prompttext": "prompt", "finding": "prompt",
            "icd10": "icd", "icd10code": "icd", "suggestedicd10": "icd",
            "icd10family": "pattern", "icd10pattern": "pattern", "pattern": "pattern", "family": "pattern",
            "cpt": "cpt", "cptcode": "cpt", "recommendedcpt": "cpt", "test": "cpt",
            "note": "note", "notes": "note", "compliancenote": "note", "compliancerule": "note",
            "source": "source", "sourcecitation": "source"}
_ICD_RE = re.compile(r"^[A-TV-Z][0-9][0-9A-Z](\.[0-9A-Z]{1,4})?$")
_PATTERN_RE = re.compile(r"^[A-TV-Z][0-9][0-9A-Z](\.[0-9A-Z]{0,4})?$")
_CPT_RE = re.compile(r"^[0-9]{4}[0-9A-Z]$")
_SYSTEMS = {s.lower(): s for s in ROS_SYSTEMS}


def _hkey(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def read_rows(file_name: str, data: bytes) -> list:
    grid = _raw_grid(file_name, data)
    header_at, colmap = None, {}
    for i, row in enumerate(grid[:10]):
        cm = {j: _HEADERS[_hkey(c)] for j, c in enumerate(row) if _hkey(c) in _HEADERS}
        if {"system", "prompt"} <= set(cm.values()):
            header_at, colmap = i, cm
            break
    if header_at is None:
        raise ImportFileError("Couldn't find the header row. Expected columns include Body System and Prompt "
                              "(download the template for the full layout).")
    rows = []
    for i in range(header_at + 1, len(grid)):
        row = grid[i]
        if not any(c.strip() for c in row):
            continue
        rec = {n: "" for n in set(_HEADERS.values())}
        for j, name in colmap.items():
            if j < len(row):
                rec[name] = row[j].strip()
        for k in ("system", "prompt", "note", "source"):       # undo the apostrophe export_csv adds to formula-looking cells
            if rec[k][:1] == "'" and rec[k][1:2] in ("=", "+", "-", "@"):
                rec[k] = rec[k][1:]
        rec["row"] = i + 1
        rows.append(rec)
    return rows


@dataclass
class RuleDraft:
    icd: str
    pattern: Optional[str]
    cpt: str
    note: Optional[str]


@dataclass
class ItemDraft:
    system: str
    prompt: str
    rules: dict = field(default_factory=dict)       # (icd, cpt) -> RuleDraft


@dataclass
class Plan:
    rows_total: int = 0
    new_items: dict = field(default_factory=dict)             # (system, prompt.lower()) -> ItemDraft
    existing_rules_to_add: dict = field(default_factory=dict)  # item_id -> {(icd, cpt): RuleDraft}
    existing_items: int = 0
    rules_already_on_file: int = 0
    repeats_in_file: int = 0
    unknown_cpt: int = 0
    skipped: list = field(default_factory=list)               # (row number, reason)

    @property
    def new_rules(self) -> int:
        return (sum(len(d.rules) for d in self.new_items.values())
                + sum(len(r) for r in self.existing_rules_to_add.values()))

    @property
    def summary_lines(self) -> list:
        out = []
        if self.existing_items:
            out.append(f"{self.existing_items} prompt(s) already in the catalog were left unchanged"
                       + (" (rules were added to them where new)." if self.existing_rules_to_add else "."))
        if self.rules_already_on_file:
            out.append(f"{self.rules_already_on_file} rule(s) already on file were skipped.")
        if self.repeats_in_file:
            out.append(f"{self.repeats_in_file} repeated row(s) in the file were merged.")
        if self.unknown_cpt:
            out.append(f"{self.unknown_cpt} rule(s) use a CPT code that isn't in the app's CPT list "
                       "(imported anyway; check the code).")
        if self.new_rules:
            out.append("All imported rules land Unreviewed; none can raise a compliance advisory until a reviewer "
                       "signs it off with a source citation.")
        return out


def build_plan(db: Session, rows: list) -> Plan:
    plan = Plan(rows_total=len(rows))
    known_cpt = {c.code for c in db.query(CptCode.code).all()}
    existing = {(i.body_system, i.prompt_text.lower()): i.id for i in db.query(RosMaster).all()}
    on_file = {(m.ros_item_id, m.suggested_icd10, m.recommended_cpt)
               for m in db.query(RosIcd10TestMapping.ros_item_id, RosIcd10TestMapping.suggested_icd10,
                                 RosIcd10TestMapping.recommended_cpt).all()}
    counted_existing = set()
    for r in rows:
        system = _SYSTEMS.get(r["system"].lower())
        prompt = " ".join(r["prompt"].split())
        if not system:
            plan.skipped.append((r["row"], "Body system isn't one of the 13 ROS systems"))
            continue
        if not prompt or len(prompt) > 255:
            plan.skipped.append((r["row"], "Prompt is empty or longer than 255 characters"))
            continue
        icd, pattern, cpt = r["icd"].upper(), (r["pattern"].upper() or None), r["cpt"].upper()
        rule = None
        if icd or cpt or pattern:
            if not (icd and cpt):
                plan.skipped.append((r["row"], "A rule needs both an ICD-10 and a CPT code"))
                continue
            if not _ICD_RE.match(icd) or (pattern and not _PATTERN_RE.match(pattern)):
                plan.skipped.append((r["row"], "ICD-10 code (or family) isn't a valid shape"))
                continue
            if not _CPT_RE.match(cpt):
                plan.skipped.append((r["row"], "CPT code isn't a valid 5-character code"))
                continue
            note = r["note"].strip()
            if r["source"].strip():
                note = (note + " " if note else "") + f"Source (not yet verified): {r['source'].strip()}"
            rule = RuleDraft(icd, pattern, cpt, note or None)
        key = (system, prompt.lower())
        item_id = existing.get(key)
        if item_id is not None:
            if key not in counted_existing:
                counted_existing.add(key)
                plan.existing_items += 1
            if rule:
                if (item_id, icd, cpt) in on_file:
                    plan.rules_already_on_file += 1
                else:
                    bucket = plan.existing_rules_to_add.setdefault(item_id, {})
                    if (icd, cpt) in bucket:
                        plan.repeats_in_file += 1
                    else:
                        bucket[(icd, cpt)] = rule
            continue
        draft = plan.new_items.setdefault(key, ItemDraft(system, prompt))
        if rule:
            if (icd, cpt) in draft.rules:
                plan.repeats_in_file += 1
            else:
                draft.rules[(icd, cpt)] = rule
    plan.unknown_cpt = sum(1 for d in plan.new_items.values() for k in d.rules if k[1] not in known_cpt) + \
        sum(1 for b in plan.existing_rules_to_add.values() for k in b if k[1] not in known_cpt)
    return plan


def apply_plan(db: Session, plan: Plan, batch: DataImportBatch, user_id: int) -> tuple:
    """Adds the planned prompts and rules inside the caller's transaction. Returns (items, rules) created."""
    next_order = dict(db.query(RosMaster.body_system, func.max(RosMaster.sort_order)).group_by(RosMaster.body_system).all())
    made_items = made_rules = 0
    records = []

    def add_rule(item_id, d):
        nonlocal made_rules
        m = RosIcd10TestMapping(ros_item_id=item_id, suggested_icd10=d.icd, icd10_pattern=d.pattern, recommended_cpt=d.cpt,
                                compliance_rule=d.note, reviewed=False)
        db.add(m)
        db.flush()
        field_audit.record_field_changes(db, "ros_icd10_test_mapping", m.id, {},
                                         {"suggested_icd10": d.icd, "recommended_cpt": d.cpt, "reviewed": False}, user_id)
        records.append(DataImportBatchRecord(batch_id=batch.id, record_table="ros_icd10_test_mapping", record_id=m.id))
        made_rules += 1

    for d in plan.new_items.values():
        order = (next_order.get(d.system) or 0) + 10
        next_order[d.system] = order
        item = RosMaster(body_system=d.system, prompt_text=d.prompt, sort_order=order, is_active=True)
        db.add(item)
        db.flush()
        field_audit.record_field_changes(db, "ros_master", item.id, {}, {"prompt_text": d.prompt, "is_active": True}, user_id)
        records.append(DataImportBatchRecord(batch_id=batch.id, record_table="ros_master", record_id=item.id))
        made_items += 1
        for rule in d.rules.values():
            add_rule(item.id, rule)
    for item_id, bucket in plan.existing_rules_to_add.items():
        for rule in bucket.values():
            add_rule(item_id, rule)
    db.add_all(records)
    batch.rows_total, batch.rows_skipped = plan.rows_total, len(plan.skipped)
    batch.patients_created, batch.recalls_created = made_items, made_rules      # columns reused: prompts / rules created
    db.flush()
    return made_items, made_rules


def undo_batch(db: Session, batch: DataImportBatch, user_id: int) -> tuple:
    """Removes the rules this batch added that are still unreviewed, then the prompts it added that are left with no
    rules and no recorded exam answers. Anything reviewed, used or that pre-dated the import stays.
    Returns (rules_removed, rules_kept, prompts_removed, prompts_kept)."""
    recs = db.query(DataImportBatchRecord).filter(DataImportBatchRecord.batch_id == batch.id).all()
    rule_ids = [r.record_id for r in recs if r.record_table == "ros_icd10_test_mapping"]
    item_ids = [r.record_id for r in recs if r.record_table == "ros_master"]
    rules_removed = rules_kept = 0
    for m in db.query(RosIcd10TestMapping).filter(RosIcd10TestMapping.id.in_(rule_ids or [0])).all():
        if m.reviewed:
            rules_kept += 1
            continue
        field_audit.record_field_changes(db, "ros_icd10_test_mapping", m.id,
            {"suggested_icd10": m.suggested_icd10, "recommended_cpt": m.recommended_cpt},
            {"suggested_icd10": None, "recommended_cpt": None}, user_id)
        db.delete(m)
        rules_removed += 1
    db.flush()
    items_removed = items_kept = 0
    for item in db.query(RosMaster).filter(RosMaster.id.in_(item_ids or [0])).all():
        has_rules = db.query(RosIcd10TestMapping.id).filter(RosIcd10TestMapping.ros_item_id == item.id).first()
        has_answers = db.query(EncounterRosResponse.id).filter(EncounterRosResponse.ros_item_id == item.id).first()
        if has_rules or has_answers:
            items_kept += 1
            continue
        field_audit.record_field_changes(db, "ros_master", item.id, {"prompt_text": item.prompt_text}, {"prompt_text": None}, user_id)
        db.delete(item)
        items_removed += 1
    db.query(DataImportBatchRecord).filter(DataImportBatchRecord.batch_id == batch.id).delete(synchronize_session=False)
    batch.status = "undone"
    batch.patients_undone, batch.patients_kept_on_undo = rules_removed, rules_kept     # reused: rules removed / kept
    batch.summary_text = ((batch.summary_text + "\n") if batch.summary_text else "") + \
        f"Undone: {rules_removed} rule(s) and {items_removed} prompt(s) removed; {rules_kept} reviewed rule(s) and {items_kept} prompt(s) in use were kept."
    return rules_removed, rules_kept, items_removed, items_kept


# ----------------------------------------------------------------------------------------- CSV out
def _safe(cell) -> str:
    s = "" if cell is None else str(cell)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s       # no spreadsheet formula injection


def template_csv() -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(TEMPLATE_COLUMNS)
    w.writerow(["Endocrine", "History of diabetes", "E11.9", "E11", "92250", "Example only: fundus photography for diabetes", "Example LCD, replace me"])
    w.writerow(["Endocrine", "History of diabetes", "E11.9", "E11", "92134", "Example only: second test for the same prompt", ""])
    w.writerow(["Ocular", "A prompt with no rule yet", "", "", "", "", ""])
    return buf.getvalue()


def export_csv(db: Session) -> str:
    """The current catalog in the import layout (plus review status), so a reviewer can work from a spreadsheet."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(TEMPLATE_COLUMNS + ["Reviewed", "Active"])
    order = {s: i for i, s in enumerate(ROS_SYSTEMS)}
    items = sorted(db.query(RosMaster).all(), key=lambda i: (order.get(i.body_system, 99), i.body_system, i.sort_order, i.id))
    for it in items:
        rules = sorted(it.mappings, key=lambda m: (m.recommended_cpt, m.suggested_icd10))
        if not rules:
            w.writerow([_safe(it.body_system), _safe(it.prompt_text), "", "", "", "", "", "", "yes" if it.is_active else "no"])
        for m in rules:
            w.writerow([_safe(it.body_system), _safe(it.prompt_text), m.suggested_icd10, m.icd10_pattern or "", m.recommended_cpt,
                        _safe(m.compliance_rule), _safe(m.source_citation if m.reviewed else ""),
                        "yes" if m.reviewed else "no", "yes" if it.is_active else "no"])
    return buf.getvalue()

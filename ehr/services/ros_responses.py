"""Granular Review-of-Systems findings recorded with an exam (ROS plan stage 4).

Writes and reads encounter_ros_responses (ehr/models/ros.py). Deliberately small and
defensive: only POSITIVE findings are stored (a ticked catalog prompt); an absent row means
"not recorded", not "denied". The eight legacy Yes/No columns on eye_exams are untouched and
remain the summary -- these rows add detail beneath them.

Exams have no edit route (create -> sign -> addenda only), so findings are written exactly
once, inside create_exam's transaction, and are therefore locked from the moment the exam
exists; there is deliberately no update/delete path here.

record_findings never raises into the caller: ROS is decision support, and a problem with it
must not block saving a clinical record. It runs in a SAVEPOINT so a failure rolls back only
the ROS rows, not the exam.
"""
import logging

from sqlalchemy.orm import Session

from ehr.models.ros import EncounterRosResponse, RosMaster

log = logging.getLogger(__name__)
MAX_FINDINGS = 200


def _parse_ids(raw_values) -> list:
    ids, seen = [], set()
    for raw in list(raw_values)[:MAX_FINDINGS * 2]:
        try:
            i = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if i > 0 and i not in seen:
            seen.add(i)
            ids.append(i)
        if len(ids) >= MAX_FINDINGS:
            break
    return ids


def record_findings(db: Session, exam_id: int, raw_values) -> int:
    """Stores a positive response for each valid, active catalog prompt id in raw_values.
    Unknown/inactive/garbled ids are ignored. Returns how many rows were written."""
    ids = _parse_ids(raw_values)
    if not ids:
        return 0
    try:
        with db.begin_nested():
            valid = [r[0] for r in db.query(RosMaster.id).filter(RosMaster.id.in_(ids), RosMaster.is_active.is_(True)).all()]
            for item_id in valid:
                db.add(EncounterRosResponse(exam_id=exam_id, ros_item_id=item_id, status="positive"))
            db.flush()
        return len(valid)
    except Exception:                      # noqa: BLE001 -- must never block saving the exam
        log.exception("Could not record ROS findings for exam %s", exam_id)
        return 0


def findings_for_exam(db: Session, exam_id: int) -> list:
    """[(body_system, [prompt_text, ...]), ...] of the exam's positive findings, in catalog order.
    Includes prompts since deactivated: the record shows what was ticked at the time."""
    try:
        rows = (db.query(RosMaster.body_system, RosMaster.prompt_text, RosMaster.sort_order)
                .join(EncounterRosResponse, EncounterRosResponse.ros_item_id == RosMaster.id)
                .filter(EncounterRosResponse.exam_id == exam_id, EncounterRosResponse.status == "positive")
                .order_by(RosMaster.body_system, RosMaster.sort_order, RosMaster.id).all())
    except Exception:                      # noqa: BLE001 -- display is best-effort
        log.exception("Could not read ROS findings for exam %s", exam_id)
        return []
    grouped = {}
    for system, prompt, _ in rows:
        grouped.setdefault(system, []).append(prompt)
    return list(grouped.items())

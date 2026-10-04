"""Medication-list import (System Administrator only): upload -> preview (counts and skipped rows by reason, no patient
names) -> confirm with a test/de-identified-data attestation -> import -> optional undo. Same safeguards as the recall
import: the uploaded bytes are held on the batch row only until the import is applied, discarded or 24 hours old. Matching,
dedupe and undo rules live in ehr/services/medication_import.py. An import is not a review: lists stay unreviewed."""
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ehr.auth import csrf
from ehr.auth.deps import get_current_user
from ehr.auth.permissions import DATA_IMPORT, ROLE_LABELS, require_role
from ehr.env_info import EHR_ENV
from ehr.models.database import get_db
from ehr.models.imports import DataImportBatch
from ehr.services import medication_import as svc
from ehr.services import recall_import as ri

router = APIRouter(prefix="/admin/medication-import", tags=["admin-medication-import"], dependencies=[Depends(require_role(*DATA_IMPORT))])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS


def _batch(db, batch_id):
    b = db.get(DataImportBatch, batch_id)
    return b if b and b.kind == svc.KIND else None


def _index(request, db, error=None, status_code=200):
    batches = db.query(DataImportBatch).filter(DataImportBatch.kind == svc.KIND).order_by(DataImportBatch.id.desc()).limit(20).all()
    return templates.TemplateResponse(request, "admin/imports/med_index.html",
        {"batches": batches, "error": error, "max_mb": ri.MAX_FILE_BYTES // 1_000_000, "columns": svc.TEMPLATE_COLUMNS}, status_code=status_code)


@router.get("", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    return _index(request, db)


@router.get("/template.csv")
def template_file():
    return Response(svc.template_csv(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="medication_import_template.csv"'})


@router.post("/upload")
async def upload(request: Request, file: UploadFile = File(...), csrf_token: str = Form(""),
                 db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    data = await file.read(ri.MAX_FILE_BYTES + 1)
    if not data:
        return _index(request, db, "Choose a file to upload.", 400)
    if len(data) > ri.MAX_FILE_BYTES:
        return _index(request, db, f"That file is larger than {ri.MAX_FILE_BYTES // 1_000_000} MB. Split it and import in parts.", 400)
    try:
        rows = svc.read_rows(file.filename or "", data)
    except ri.ImportFileError as exc:
        return _index(request, db, str(exc), 400)
    if not rows:
        return _index(request, db, "No rows were found under the header.", 400)
    batch = DataImportBatch(kind=svc.KIND, file_name=(file.filename or "")[:255], status="staged", file_bytes=data,
                            uploaded_by_user_id=user.id, rows_total=len(rows))
    db.add(batch)
    db.commit()
    return RedirectResponse(f"/admin/medication-import/{batch.id}", status_code=303)


@router.get("/{batch_id}", response_class=HTMLResponse)
def batch_detail(request: Request, batch_id: int, db: Session = Depends(get_db)):
    batch = _batch(db, batch_id)
    if not batch:
        return HTMLResponse("Not found", status_code=404)
    ctx = {"batch": batch, "error": request.query_params.get("error"), "plan": None, "skipped_by_reason": {}}
    if batch.status == "staged" and batch.file_bytes:
        try:
            plan = svc.build_plan(db, svc.read_rows(batch.file_name or "", batch.file_bytes))
        except ri.ImportFileError as exc:
            return _index(request, db, str(exc), 400)
        grouped = {}
        for row, reason in plan.skipped:
            grouped.setdefault(reason, []).append(row)
        ctx.update(plan=plan, skipped_by_reason={k: (len(v), v[:25]) for k, v in grouped.items()})
    return templates.TemplateResponse(request, "admin/imports/med_batch.html", ctx)


@router.post("/{batch_id}/confirm")
def confirm(request: Request, batch_id: int, attest: str = Form(""), reason: str = Form(""), csrf_token: str = Form(""),
            db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    batch = _batch(db, batch_id)
    if not batch:
        return HTMLResponse("Not found", status_code=404)
    if batch.status != "staged" or not batch.file_bytes:
        return RedirectResponse(f"/admin/medication-import/{batch_id}", status_code=303)
    reason = reason.strip()
    if attest != "on" or len(reason) < 5:
        return RedirectResponse(f"/admin/medication-import/{batch_id}?error=Tick the confirmation and give a reason (at least a few words) before importing.", status_code=303)
    try:
        plan = svc.build_plan(db, svc.read_rows(batch.file_name or "", batch.file_bytes))
        svc.apply_plan(db, plan, batch, user.id)
        batch.status, batch.imported_at, batch.attestation_reason = "imported", datetime.utcnow(), reason[:500]
        batch.summary_text = "\n".join(plan.summary_lines) or None
        batch.file_bytes = None
        db.commit()
    except Exception:                                # noqa: BLE001 -- nothing partial is kept
        db.rollback()
        batch = _batch(db, batch_id)
        batch.status, batch.file_bytes = "failed", None
        batch.summary_text = "The import failed and nothing was saved. Try again, or contact support if it keeps happening."
        db.commit()
    return RedirectResponse(f"/admin/medication-import/{batch_id}", status_code=303)


@router.post("/{batch_id}/discard")
def discard(request: Request, batch_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    batch = _batch(db, batch_id)
    if batch and batch.status == "staged":
        db.delete(batch)
        db.commit()
    return RedirectResponse("/admin/medication-import", status_code=303)


@router.post("/{batch_id}/undo")
def undo(request: Request, batch_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    batch = _batch(db, batch_id)
    if not batch:
        return HTMLResponse("Not found", status_code=404)
    if batch.status == "imported":
        svc.undo_batch(db, batch, user.id)
        db.commit()
    return RedirectResponse(f"/admin/medication-import/{batch_id}", status_code=303)

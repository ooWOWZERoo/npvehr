"""Recall-report import (System Administrator only): upload -> preview (counts and warnings, no patient names) ->
confirm with a test/de-identified-data attestation -> import -> optional undo. Stateless on Vercel by staging the
uploaded bytes on the batch row only until the import is applied, discarded or 24 hours old; the PHI-bearing file
is not kept afterwards. See ehr/services/recall_import.py for the column mapping and matching rules, and the
batch/recall tables in ehr/models/imports.py."""
from collections import Counter
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ehr.auth import csrf
from ehr.auth.deps import get_current_user
from ehr.auth.permissions import DATA_IMPORT, ROLE_LABELS, require_role
from ehr.env_info import EHR_ENV
from ehr.models.database import get_db
from ehr.models.imports import DataImportBatch
from ehr.services import recall_import as ri

router = APIRouter(prefix="/admin/imports", tags=["admin-imports"], dependencies=[Depends(require_role(*DATA_IMPORT))])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS


def _purge_stale(db: Session) -> None:
    cutoff = datetime.utcnow() - timedelta(hours=24)
    db.query(DataImportBatch).filter(DataImportBatch.status == "staged", DataImportBatch.created_at < cutoff).delete(synchronize_session=False)
    db.commit()


def _index(request, db, error=None, status_code=200):
    batches = db.query(DataImportBatch).filter(DataImportBatch.kind == "recall_report").order_by(DataImportBatch.id.desc()).limit(20).all()
    return templates.TemplateResponse(request, "admin/imports/index.html",
        {"batches": batches, "error": error, "max_mb": ri.MAX_FILE_BYTES // 1_000_000}, status_code=status_code)


@router.get("", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    _purge_stale(db)
    return _index(request, db)


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
        rows = ri.read_rows(file.filename or "", data)
    except ri.ImportFileError as exc:
        return _index(request, db, str(exc), 400)
    if not rows:
        return _index(request, db, "No patient rows were found in that file.", 400)
    batch = DataImportBatch(kind="recall_report", file_name=(file.filename or "")[:255], status="staged",
                            file_bytes=data, uploaded_by_user_id=user.id, rows_total=len(rows))
    db.add(batch)
    db.commit()
    return RedirectResponse(f"/admin/imports/{batch.id}", status_code=303)


@router.get("/{batch_id}", response_class=HTMLResponse)
def batch_detail(request: Request, batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(DataImportBatch, batch_id)
    if not batch:
        return HTMLResponse("Not found", status_code=404)
    if batch.kind == "ros_catalog":
        return RedirectResponse(f"/admin/ros/import/{batch_id}", status_code=303)
    ctx = {"batch": batch, "error": request.query_params.get("error"), "plan": None, "skipped_by_reason": {}}
    if batch.status == "staged" and batch.file_bytes:
        try:
            plan = ri.build_plan(db, ri.read_rows(batch.file_name or "", batch.file_bytes))
        except ri.ImportFileError as exc:
            return _index(request, db, str(exc), 400)
        grouped = {}
        for row, reason in plan.skipped:
            grouped.setdefault(reason, []).append(row)
        ctx.update(plan=plan, skipped_by_reason={k: (len(v), v[:25]) for k, v in grouped.items()})
    return templates.TemplateResponse(request, "admin/imports/batch.html", ctx)


@router.post("/{batch_id}/confirm")
def confirm(request: Request, batch_id: int, attest: str = Form(""), reason: str = Form(""), csrf_token: str = Form(""),
            db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    batch = db.get(DataImportBatch, batch_id)
    if not batch:
        return HTMLResponse("Not found", status_code=404)
    if batch.status != "staged" or not batch.file_bytes:
        return RedirectResponse(f"/admin/imports/{batch_id}", status_code=303)
    reason = reason.strip()
    if attest != "on" or len(reason) < 5:
        return RedirectResponse(f"/admin/imports/{batch_id}?error=Tick the confirmation and give a reason (at least a few words) before importing.", status_code=303)
    try:
        plan = ri.build_plan(db, ri.read_rows(batch.file_name or "", batch.file_bytes))
        ri.apply_plan(db, plan, batch)
        batch.status, batch.imported_at, batch.attestation_reason = "imported", datetime.utcnow(), reason[:500]
        batch.summary_text = "\n".join(plan.summary_lines) or None
        batch.file_bytes = None                      # the upload is not kept once applied
        db.commit()
    except Exception:                                # noqa: BLE001 -- nothing partial is kept
        db.rollback()
        batch = db.get(DataImportBatch, batch_id)
        batch.status, batch.file_bytes = "failed", None
        batch.summary_text = "The import failed and nothing was saved. Try again, or contact support if it keeps happening."
        db.commit()
    return RedirectResponse(f"/admin/imports/{batch_id}", status_code=303)


@router.post("/{batch_id}/discard")
def discard(request: Request, batch_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    batch = db.get(DataImportBatch, batch_id)
    if batch and batch.status == "staged":
        db.delete(batch)
        db.commit()
    return RedirectResponse("/admin/imports", status_code=303)


@router.post("/{batch_id}/undo")
def undo(request: Request, batch_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    batch = db.get(DataImportBatch, batch_id)
    if not batch:
        return HTMLResponse("Not found", status_code=404)
    if batch.status == "imported":
        ri.undo_batch(db, batch)
        db.commit()
    return RedirectResponse(f"/admin/imports/{batch_id}", status_code=303)

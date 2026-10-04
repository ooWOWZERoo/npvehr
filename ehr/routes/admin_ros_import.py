"""ROS catalog bulk import (System/Practice Administrator): upload a .csv/.xls of prompts and rules -> preview
(counts, skipped rows with reasons) -> confirm -> optional undo. Everything lands Unreviewed; sign-off stays in
/admin/ros. Also serves the blank template and an export of the current catalog in the same layout. Parsing,
matching and undo rules live in ehr/services/ros_import.py; batches use the shared data_import_batches table."""
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ehr.auth import csrf
from ehr.auth.deps import get_current_user
from ehr.auth.permissions import ROLE_LABELS, ROS_CATALOG_EDIT, ROS_CATALOG_VIEW, require_role
from ehr.env_info import EHR_ENV
from ehr.models.database import get_db
from ehr.models.imports import DataImportBatch
from ehr.services import recall_import as ri
from ehr.services import ros_import as svc

router = APIRouter(prefix="/admin/ros/import", tags=["admin-ros-import"])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS
_EDIT = [Depends(require_role(*ROS_CATALOG_EDIT))]


def _batch(db, batch_id):
    b = db.get(DataImportBatch, batch_id)
    return b if b and b.kind == svc.KIND else None


def _index(request, db, error=None, status_code=200):
    batches = (db.query(DataImportBatch).filter(DataImportBatch.kind == svc.KIND)
               .order_by(DataImportBatch.id.desc()).limit(20).all())
    return templates.TemplateResponse(request, "admin/ros/import_index.html",
        {"batches": batches, "error": error, "max_mb": ri.MAX_FILE_BYTES // 1_000_000, "columns": svc.TEMPLATE_COLUMNS},
        status_code=status_code)


def _csv(text, name):
    return Response(text, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("", response_class=HTMLResponse, dependencies=_EDIT)
def index(request: Request, db: Session = Depends(get_db)):
    return _index(request, db)


@router.get("/template.csv", dependencies=_EDIT)
def template_file():
    return _csv(svc.template_csv(), "ros_import_template.csv")


@router.get("/export.csv", dependencies=[Depends(require_role(*ROS_CATALOG_VIEW))])
def export_file(db: Session = Depends(get_db)):
    return _csv(svc.export_csv(db), "ros_catalog.csv")


@router.post("/upload", dependencies=_EDIT)
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
    return RedirectResponse(f"/admin/ros/import/{batch.id}", status_code=303)


@router.get("/{batch_id}", response_class=HTMLResponse, dependencies=_EDIT)
def batch_detail(request: Request, batch_id: int, db: Session = Depends(get_db)):
    batch = _batch(db, batch_id)
    if not batch:
        return HTMLResponse("Not found", status_code=404)
    ctx = {"batch": batch, "error": request.query_params.get("error"), "plan": None}
    if batch.status == "staged" and batch.file_bytes:
        try:
            ctx["plan"] = svc.build_plan(db, svc.read_rows(batch.file_name or "", batch.file_bytes))
        except ri.ImportFileError as exc:
            return _index(request, db, str(exc), 400)
    return templates.TemplateResponse(request, "admin/ros/import_batch.html", ctx)


@router.post("/{batch_id}/confirm", dependencies=_EDIT)
def confirm(request: Request, batch_id: int, csrf_token: str = Form(""),
            db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    batch = _batch(db, batch_id)
    if not batch:
        return HTMLResponse("Not found", status_code=404)
    if batch.status != "staged" or not batch.file_bytes:
        return RedirectResponse(f"/admin/ros/import/{batch_id}", status_code=303)
    try:
        plan = svc.build_plan(db, svc.read_rows(batch.file_name or "", batch.file_bytes))
        svc.apply_plan(db, plan, batch, user.id)
        batch.status, batch.imported_at = "imported", datetime.utcnow()
        batch.summary_text = "\n".join(plan.summary_lines) or None
        batch.file_bytes = None
        db.commit()
    except Exception:                                # noqa: BLE001 -- nothing partial is kept
        db.rollback()
        batch = _batch(db, batch_id)
        batch.status, batch.file_bytes = "failed", None
        batch.summary_text = "The import failed and nothing was saved. Try again, or contact support if it keeps happening."
        db.commit()
    return RedirectResponse(f"/admin/ros/import/{batch_id}", status_code=303)


@router.post("/{batch_id}/discard", dependencies=_EDIT)
def discard(request: Request, batch_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    batch = _batch(db, batch_id)
    if batch and batch.status == "staged":
        db.delete(batch)
        db.commit()
    return RedirectResponse("/admin/ros/import", status_code=303)


@router.post("/{batch_id}/undo", dependencies=_EDIT)
def undo(request: Request, batch_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db),
         user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    batch = _batch(db, batch_id)
    if not batch:
        return HTMLResponse("Not found", status_code=404)
    if batch.status == "imported":
        svc.undo_batch(db, batch, user.id)
        db.commit()
    return RedirectResponse(f"/admin/ros/import/{batch_id}", status_code=303)

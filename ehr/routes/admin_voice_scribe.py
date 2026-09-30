"""Voice Scribe vocabulary correction table administration (user request:
"is there a way to expose the correction-dictionary to the admin so the
admin can add/edit/remove/update vision terms" -- the client-side WASM
Whisper model can't be trained/fine-tuned, so this table is the only real
lever staff have over transcription accuracy for ocular/optometric terms;
see VoiceScribeCorrection's own docstring in ehr/models/database.py.
Route/permission/audit shape mirrors Service & Fee Catalog (admin_billing.py)
-- a flat list with inline add, a dedicated edit page, and a toggle-active
action -- plus a real delete, since a text correction (unlike a service)
has no downstream records that would be orphaned by removing it.

The corrections.json endpoint below is the one route in this file NOT
gated by VOICE_SCRIBE_VOCAB_EDIT: it's read by every doctor's browser
(voice_scribe.js) to apply corrections during dictation, not just admins,
so it only requires being logged in (enforced at router-inclusion time in
ehr/app.py, same as every other route here)."""
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ehr.models.database import get_db, VoiceScribeCorrection
from ehr.services import field_audit
from ehr.env_info import EHR_ENV
from ehr.auth.permissions import require_role, VOICE_SCRIBE_VOCAB_EDIT, ROLE_LABELS
from ehr.auth.deps import get_current_user
from ehr.auth import csrf

router = APIRouter(prefix="/admin/voice-scribe", tags=["admin-voice-scribe"])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS

CORRECTION_AUDITED_FIELDS = ["phrase", "correction"]


@router.get("/corrections", response_class=HTMLResponse, dependencies=[Depends(require_role(*VOICE_SCRIBE_VOCAB_EDIT))])
def list_corrections(request: Request, db: Session = Depends(get_db)):
    corrections = db.query(VoiceScribeCorrection).order_by(VoiceScribeCorrection.phrase).all()
    return templates.TemplateResponse(request, "admin/voice_scribe/corrections_list.html", {"corrections": corrections})


@router.post("/corrections/new", dependencies=[Depends(require_role(*VOICE_SCRIBE_VOCAB_EDIT))])
def create_correction(request: Request, phrase: str = Form(...), correction: str = Form(...),
    csrf_token: str = Form(""), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    phrase = phrase.strip().lower()
    correction = correction.strip()
    if not phrase or not correction:
        return HTMLResponse("Both phrase and correction are required.", status_code=400)
    if db.query(VoiceScribeCorrection).filter(VoiceScribeCorrection.phrase == phrase).first():
        return HTMLResponse(f"A correction for '{phrase}' already exists.", status_code=400)
    db.add(VoiceScribeCorrection(phrase=phrase, correction=correction, active=True))
    db.commit()
    return RedirectResponse("/admin/voice-scribe/corrections", status_code=303)


@router.get("/corrections/{correction_id}/edit", response_class=HTMLResponse,
    dependencies=[Depends(require_role(*VOICE_SCRIBE_VOCAB_EDIT))])
def edit_correction_form(request: Request, correction_id: int, db: Session = Depends(get_db)):
    correction = db.query(VoiceScribeCorrection).filter(VoiceScribeCorrection.id == correction_id).first()
    if not correction:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "admin/voice_scribe/correction_form.html",
        {"correction": correction, "error": None})


@router.post("/corrections/{correction_id}/edit", dependencies=[Depends(require_role(*VOICE_SCRIBE_VOCAB_EDIT))])
def update_correction(request: Request, correction_id: int, phrase: str = Form(...), correction: str = Form(...),
    csrf_token: str = Form(""), db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    row = db.query(VoiceScribeCorrection).filter(VoiceScribeCorrection.id == correction_id).first()
    if not row:
        return HTMLResponse("Not found", status_code=404)
    phrase = phrase.strip().lower()
    correction = correction.strip()
    if not phrase or not correction:
        return templates.TemplateResponse(request, "admin/voice_scribe/correction_form.html",
            {"correction": row, "error": "Both phrase and correction are required."}, status_code=400)
    conflict = db.query(VoiceScribeCorrection).filter(
        VoiceScribeCorrection.phrase == phrase, VoiceScribeCorrection.id != correction_id).first()
    if conflict:
        return templates.TemplateResponse(request, "admin/voice_scribe/correction_form.html",
            {"correction": row, "error": f"A correction for '{phrase}' already exists."}, status_code=400)
    before = {f: getattr(row, f) for f in CORRECTION_AUDITED_FIELDS}
    row.phrase = phrase
    row.correction = correction
    after = {f: getattr(row, f) for f in CORRECTION_AUDITED_FIELDS}
    field_audit.record_field_changes(db, "voice_scribe_corrections", correction_id, before, after, user.id)
    db.commit()
    return RedirectResponse("/admin/voice-scribe/corrections", status_code=303)


@router.post("/corrections/{correction_id}/toggle-active", dependencies=[Depends(require_role(*VOICE_SCRIBE_VOCAB_EDIT))])
def toggle_correction_active(request: Request, correction_id: int, csrf_token: str = Form(""),
    db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    row = db.query(VoiceScribeCorrection).filter(VoiceScribeCorrection.id == correction_id).first()
    if not row:
        return HTMLResponse("Not found", status_code=404)
    before_active = row.active
    row.active = not row.active
    field_audit.record_field_changes(db, "voice_scribe_corrections", correction_id,
        {"active": before_active}, {"active": row.active}, user.id)
    db.commit()
    return RedirectResponse("/admin/voice-scribe/corrections", status_code=303)


@router.post("/corrections/{correction_id}/delete", dependencies=[Depends(require_role(*VOICE_SCRIBE_VOCAB_EDIT))])
def delete_correction(request: Request, correction_id: int, csrf_token: str = Form(""), db: Session = Depends(get_db)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    row = db.query(VoiceScribeCorrection).filter(VoiceScribeCorrection.id == correction_id).first()
    if row:
        db.delete(row)
        db.commit()
    return RedirectResponse("/admin/voice-scribe/corrections", status_code=303)


@router.get("/corrections.json")
def corrections_json(db: Session = Depends(get_db)):
    """Fetched by ehr/static/js/voice_scribe.js on load. Any logged-in staff
    member, not just admins -- see this module's own docstring."""
    rows = db.query(VoiceScribeCorrection).filter(VoiceScribeCorrection.active.is_(True)).all()
    return JSONResponse({row.phrase: row.correction for row in rows})

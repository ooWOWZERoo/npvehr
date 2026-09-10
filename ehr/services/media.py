"""Patient photo storage.

Two backends, selected purely by whether CLOUDINARY_URL is set in the
environment:

- Local disk (default, for local dev): files land in ehr/static/uploads/,
  same as the original baseline behavior. Unusable on Vercel, whose
  filesystem is read-only/ephemeral outside /tmp -- uploaded files would
  vanish on the next cold start.
- Cloudinary (when CLOUDINARY_URL is set, e.g. in deployed environments):
  files are uploaded with delivery type "authenticated" (NOT the default
  "upload" type, which is a permanently public URL) -- nothing about the
  stored public_id resolves to a working image URL without our api_secret
  to sign it.

Patient.photo_path stores an opaque marker either way ("/static/uploads/..."
or "cloudinary:<public_id>") -- never a browser-facing URL. Browsers never
talk to Cloudinary directly for patient photos: every request goes through
the auth-gated GET /patients/{id}/photo route (ehr/routes/patients.py),
which resolves the marker to actual bytes server-side. This keeps photo
access under the same session-auth model as every other patient record,
rather than "anyone holding the URL, forever" (the gap noted in the
baseline spec, §15.1, before this).
"""
import mimetypes
import os
import urllib.request
import uuid
from typing import Optional, Tuple

from fastapi import UploadFile

LOCAL_UPLOAD_DIR = os.path.join("ehr", "static", "uploads")
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
CLOUDINARY_FOLDER = "npvehr/patients"
CLOUDINARY_MARKER_PREFIX = "cloudinary:"


def _cloudinary_configured() -> bool:
    return bool(os.environ.get("CLOUDINARY_URL"))


def save_patient_photo(upload: UploadFile) -> Optional[str]:
    """Save an uploaded patient photo, returning the opaque marker to store
    on the Patient row, or None if there's nothing valid to save."""
    if not upload or not upload.filename:
        return None
    ext = os.path.splitext(upload.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return None

    if _cloudinary_configured():
        import cloudinary.uploader
        public_id = f"{CLOUDINARY_FOLDER}/{uuid.uuid4().hex}"
        cloudinary.uploader.upload(upload.file, public_id=public_id, overwrite=False,
                                    type="authenticated")
        return f"{CLOUDINARY_MARKER_PREFIX}{public_id}"

    os.makedirs(LOCAL_UPLOAD_DIR, exist_ok=True)
    fname = f"{uuid.uuid4().hex}{ext}"
    dest = os.path.join(LOCAL_UPLOAD_DIR, fname)
    with open(dest, "wb") as f:
        f.write(upload.file.read())
    return f"/static/uploads/{fname}"


def get_photo_bytes(photo_path: Optional[str]) -> Optional[Tuple[bytes, str]]:
    """Resolve a stored marker to (bytes, content_type), or None if missing/
    unreadable. Called only from the auth-gated photo route -- this is the
    one place that actually fetches/reads photo content."""
    if not photo_path:
        return None
    if photo_path.startswith("http://") or photo_path.startswith("https://"):
        # Backward compatibility only: a small pre-existing window of this app's
        # life stored the full (public, type="upload") Cloudinary secure_url
        # directly. Any such row still works by fetching it as-is; no new
        # photos are ever saved in this format.
        try:
            with urllib.request.urlopen(photo_path, timeout=10) as resp:
                data = resp.read()
                content_type = resp.headers.get_content_type() or "application/octet-stream"
                return data, content_type
        except Exception:
            return None
    if photo_path.startswith(CLOUDINARY_MARKER_PREFIX):
        public_id = photo_path[len(CLOUDINARY_MARKER_PREFIX):]
        import cloudinary.utils
        signed_url, _ = cloudinary.utils.cloudinary_url(public_id, type="authenticated", sign_url=True)
        try:
            with urllib.request.urlopen(signed_url, timeout=10) as resp:
                data = resp.read()
                content_type = resp.headers.get_content_type() or "application/octet-stream"
                return data, content_type
        except Exception:
            return None
    if photo_path.startswith("/static/uploads/"):
        fname = os.path.basename(photo_path)
        full_path = os.path.join(LOCAL_UPLOAD_DIR, fname)
        if os.path.commonpath([os.path.abspath(full_path), os.path.abspath(LOCAL_UPLOAD_DIR)]) != os.path.abspath(LOCAL_UPLOAD_DIR):
            return None
        if not os.path.isfile(full_path):
            return None
        content_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"
        with open(full_path, "rb") as f:
            return f.read(), content_type
    return None


def delete_patient_photo(photo_path: Optional[str]):
    """Best-effort delete of a previously-saved patient photo, whichever backend
    it lives in. Used when a patient's photo is replaced, so old files don't
    accumulate forever. Deliberately never raises: a missing file, a permissions
    issue, or an unrecognized marker all just result in the delete being skipped
    rather than the request failing -- losing a stale file is far less harmful
    than a 500 on save."""
    if not photo_path:
        return
    if photo_path.startswith("/static/uploads/"):
        fname = os.path.basename(photo_path)
        full_path = os.path.join(LOCAL_UPLOAD_DIR, fname)
        try:
            # Defensive check: resolved path must still be inside LOCAL_UPLOAD_DIR.
            if os.path.commonpath([os.path.abspath(full_path), os.path.abspath(LOCAL_UPLOAD_DIR)]) != os.path.abspath(LOCAL_UPLOAD_DIR):
                return
            if os.path.isfile(full_path):
                os.remove(full_path)
        except OSError:
            pass
        return
    if photo_path.startswith(CLOUDINARY_MARKER_PREFIX) and _cloudinary_configured():
        public_id = photo_path[len(CLOUDINARY_MARKER_PREFIX):]
        try:
            import cloudinary.uploader
            cloudinary.uploader.destroy(public_id, type="authenticated")
        except Exception:
            pass  # best-effort; never let cleanup failure block saving the patient record

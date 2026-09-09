"""Patient photo storage.

Two backends, selected purely by whether CLOUDINARY_URL is set in the
environment:

- Local disk (default, for local dev): files land in ehr/static/uploads/,
  same as the original baseline behavior. Unusable on Vercel, whose
  filesystem is read-only/ephemeral outside /tmp -- uploaded files would
  vanish on the next cold start.
- Cloudinary (when CLOUDINARY_URL is set, e.g. in deployed environments):
  files are uploaded to Cloudinary and the returned secure (https) URL is
  stored directly in Patient.photo_path, so templates render it exactly
  like a local path -- no template changes needed either way.

The cloudinary SDK reads CLOUDINARY_URL from the environment automatically
(cloudinary://<api_key>:<api_secret>@<cloud_name>), so no explicit
cloudinary.config() call is needed here beyond importing it.
"""
import os
import re
import uuid
from typing import Optional

from fastapi import UploadFile

LOCAL_UPLOAD_DIR = os.path.join("ehr", "static", "uploads")
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
CLOUDINARY_FOLDER = "npvehr/patients"


def _cloudinary_configured() -> bool:
    return bool(os.environ.get("CLOUDINARY_URL"))


def save_patient_photo(upload: UploadFile) -> Optional[str]:
    """Save an uploaded patient photo, returning the path/URL to store on the
    Patient row, or None if there's nothing valid to save."""
    if not upload or not upload.filename:
        return None
    ext = os.path.splitext(upload.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return None

    if _cloudinary_configured():
        import cloudinary.uploader
        public_id = f"{CLOUDINARY_FOLDER}/{uuid.uuid4().hex}"
        result = cloudinary.uploader.upload(upload.file, public_id=public_id, overwrite=False)
        return result["secure_url"]

    os.makedirs(LOCAL_UPLOAD_DIR, exist_ok=True)
    fname = f"{uuid.uuid4().hex}{ext}"
    dest = os.path.join(LOCAL_UPLOAD_DIR, fname)
    with open(dest, "wb") as f:
        f.write(upload.file.read())
    return f"/static/uploads/{fname}"


def _cloudinary_public_id_from_url(url: str) -> Optional[str]:
    """Extract the public_id from a Cloudinary delivery URL, e.g.
    https://res.cloudinary.com/<cloud>/image/upload/v169.../npvehr/patients/<id>.jpg
    -> npvehr/patients/<id>. Returns None if the URL doesn't look right."""
    match = re.search(r"/upload/(?:v\d+/)?(.+)\.[A-Za-z0-9]+$", url)
    return match.group(1) if match else None


def delete_patient_photo(photo_path: Optional[str]):
    """Best-effort delete of a previously-saved patient photo, whichever backend
    it lives in. Used when a patient's photo is replaced, so old files don't
    accumulate forever. Deliberately never raises: a missing file, a permissions
    issue, or an unrecognized URL all just result in the delete being skipped
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
    if _cloudinary_configured() and "cloudinary.com" in photo_path:
        public_id = _cloudinary_public_id_from_url(photo_path)
        if not public_id:
            return
        try:
            import cloudinary.uploader
            cloudinary.uploader.destroy(public_id)
        except Exception:
            pass  # best-effort; never let cleanup failure block saving the patient record

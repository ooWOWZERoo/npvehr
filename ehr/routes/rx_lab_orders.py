"""Real Order Management: a signed Prescription's optical lab fulfillment,
tracked from placement through fabrication, shipping, receiving, and
dispensing to the patient. Replaces the "/orders/" sidebar placeholder that
used to live in ehr/routes/store_ops.py.

See RxLabOrder's docstring (ehr/models/database.py) for why this is a
separate table/lifecycle from DiagnosticOrder (clinical testing orders).
"""
from datetime import datetime
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from ehr.models.database import get_db, RxLabOrder, Prescription
from ehr.env_info import EHR_ENV
from ehr.auth.deps import get_current_user
from ehr.auth.permissions import require_role, RX_LAB_ORDER_VIEW, RX_LAB_ORDER_EDIT, ROLE_LABELS
from ehr.auth import csrf
from ehr.services import rx_lab_orders as lab_orders

router = APIRouter(prefix="/orders", tags=["rx-lab-orders"])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS

OPEN_STATUSES = ["placed", "in_fabrication", "shipped", "received"]


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

# A handful of representative labs, same narrow-hardcoded-list posture as
# this app's other small lookup tables (ICD-10, CPT) rather than a full
# vendor-management catalog -- staff can also type a lab name not in this
# list via the "Other" free-text fallback in the form.
COMMON_LABS = ["Essilor Lab", "Vision Ease", "Walman Optical", "Luzerne Optical", "Local In-House Lab"]


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(require_role(*RX_LAB_ORDER_VIEW))])
def order_list(request: Request, status: str = None, db: Session = Depends(get_db)):
    q = db.query(RxLabOrder)
    if status:
        q = q.filter(RxLabOrder.status == status)
    orders = q.order_by(RxLabOrder.placed_at.desc()).limit(200).all()
    open_count = db.query(RxLabOrder).filter(RxLabOrder.status.in_(OPEN_STATUSES)).count()
    return templates.TemplateResponse(request, "orders/list.html", {
        "orders": orders, "status_filter": status or "", "open_count": open_count})


def _form_page(request, rx, values=None, errors=None, status_code=200, **extra):
    """The place-order page. `values` re-fills the form after a validation error; a first visit pre-fills the lens design from the Rx."""
    values = values if values is not None else {"order_type": rx.rx_type, "lens_design": rx.lens_type if rx.lens_type in lab_orders.LENS_DESIGNS else ""}
    return templates.TemplateResponse(request, "orders/form.html", {
        "rx": rx, "common_labs": COMMON_LABS, "values": values, "errors": errors or [], "lens_designs": lab_orders.LENS_DESIGNS, **extra},
        status_code=status_code)


def _measurements_from(form):
    """The glasses measurements fields as submitted (strings), for validation or to re-fill the form."""
    return {k: (form.get(k) or "").strip() for k in lab_orders.MEASURE_FIELDS}


@router.get("/new", response_class=HTMLResponse, dependencies=[Depends(require_role(*RX_LAB_ORDER_EDIT))])
def new_order_form(request: Request, rx_id: int, db: Session = Depends(get_db)):
    rx = db.query(Prescription).filter(Prescription.id == rx_id).first()
    if not rx: return HTMLResponse("Not found", status_code=404)
    if not rx.signed_at:
        return HTMLResponse("Only a signed prescription can have a lab order placed against it.", status_code=400)
    return _form_page(request, rx)


@router.post("/new", dependencies=[Depends(require_role(*RX_LAB_ORDER_EDIT))])
async def create_order(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user)):
    form = await request.form()
    csrf.verify_or_403(request.state.csrf_token, form.get("csrf_token"))
    rx = db.query(Prescription).filter(Prescription.id == _int(form.get("rx_id"))).first()
    if not rx: return HTMLResponse("Not found", status_code=404)
    if not rx.signed_at:
        return HTMLResponse("Only a signed prescription can have a lab order placed against it.", status_code=400)
    order_type, lab_name = form.get("order_type", ""), (form.get("lab_name") or "").strip()
    if order_type not in ("glasses", "contacts") or not lab_name:
        return HTMLResponse("An order type (glasses or contacts) and a lab are required.", status_code=400)
    measurements = dict(lab_orders.NO_MEASUREMENTS)              # a contact lens order records no PD, seg height or lens design
    if order_type == "glasses":
        try:
            measurements = lab_orders.clean_glasses_measurements(*(_measurements_from(form)[k] for k in lab_orders.MEASURE_FIELDS))
        except lab_orders.MeasurementError as e:
            values = {"order_type": order_type, "lab_name": lab_name, "eta_date": form.get("eta_date", ""), "notes": form.get("notes", ""),
                      **_measurements_from(form)}
            return _form_page(request, rx, values=values, errors=e.errors, status_code=400)
    order = RxLabOrder(rx_id=rx.id, patient_id=rx.patient_id, order_type=order_type, lab_name=lab_name,
                        eta_date=form.get("eta_date") or None, notes=form.get("notes") or None, placed_by_user_id=user.id, **measurements)
    db.add(order); db.commit(); db.refresh(order)
    return RedirectResponse(f"/orders/{order.id}", status_code=303)


@router.get("/{order_id}", response_class=HTMLResponse, dependencies=[Depends(require_role(*RX_LAB_ORDER_VIEW))])
def order_detail(request: Request, order_id: int, db: Session = Depends(get_db)):
    order = db.query(RxLabOrder).filter(RxLabOrder.id == order_id).first()
    if not order: return HTMLResponse("Not found", status_code=404)
    remakes = db.query(RxLabOrder).filter(RxLabOrder.remake_of_order_id == order_id).order_by(RxLabOrder.placed_at).all()
    return templates.TemplateResponse(request, "orders/detail.html", {
        "order": order, "remakes": remakes,
        "next_statuses": sorted(lab_orders.ALLOWED_TRANSITIONS.get(order.status, set()) - {"cancelled"})})


@router.post("/{order_id}/transition", dependencies=[Depends(require_role(*RX_LAB_ORDER_EDIT))])
def transition_order(request: Request, order_id: int, new_status: str = Form(...),
                      cancelled_reason: str = Form(""), csrf_token: str = Form(""),
                      db: Session = Depends(get_db), user=Depends(get_current_user)):
    csrf.verify_or_403(request.state.csrf_token, csrf_token)
    order = db.query(RxLabOrder).filter(RxLabOrder.id == order_id).first()
    if not order: return HTMLResponse("Not found", status_code=404)
    try:
        lab_orders.transition(order, new_status, dispensed_by_user_id=user.id,
                               cancelled_reason=cancelled_reason or None)
    except ValueError as e:
        return HTMLResponse(str(e), status_code=400)
    db.commit()
    return RedirectResponse(f"/orders/{order_id}", status_code=303)


def _remake_page(request, original, values=None, errors=None, status_code=200):
    values = values if values is not None else {k: ("" if getattr(original, k) is None else getattr(original, k)) for k in lab_orders.MEASURE_FIELDS}
    return templates.TemplateResponse(request, "orders/remake.html", {
        "order": original, "values": values, "errors": errors or [], "lens_designs": lab_orders.LENS_DESIGNS}, status_code=status_code)


@router.get("/{order_id}/remake", response_class=HTMLResponse, dependencies=[Depends(require_role(*RX_LAB_ORDER_EDIT))])
def remake_form(request: Request, order_id: int, db: Session = Depends(get_db)):
    """A glasses remake is still an order, so it needs its own PD (and seg height where required): the form opens pre-filled from the
    order it redoes. (A contact lens remake has no measurements and is placed straight from the order page.)"""
    original = db.query(RxLabOrder).filter(RxLabOrder.id == order_id).first()
    if not original: return HTMLResponse("Not found", status_code=404)
    if original.order_type != "glasses":
        return RedirectResponse(f"/orders/{order_id}", status_code=303)
    return _remake_page(request, original)


@router.post("/{order_id}/remake", dependencies=[Depends(require_role(*RX_LAB_ORDER_EDIT))])
async def remake_order(request: Request, order_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Reorder/remake tracking: a new order row linked back to the one it's
    redoing, rather than mutating the original -- the original's own history
    (what was sent, when, to which lab, why it came back) stays intact."""
    form = await request.form()
    csrf.verify_or_403(request.state.csrf_token, form.get("csrf_token"))
    original = db.query(RxLabOrder).filter(RxLabOrder.id == order_id).first()
    if not original: return HTMLResponse("Not found", status_code=404)
    measurements = dict(lab_orders.NO_MEASUREMENTS)
    if original.order_type == "glasses":
        try:
            measurements = lab_orders.clean_glasses_measurements(*(_measurements_from(form)[k] for k in lab_orders.MEASURE_FIELDS))
        except lab_orders.MeasurementError as e:
            return _remake_page(request, original, values=_measurements_from(form), errors=e.errors, status_code=400)
    remake = RxLabOrder(rx_id=original.rx_id, patient_id=original.patient_id, order_type=original.order_type,
                         lab_name=original.lab_name, placed_by_user_id=user.id, remake_of_order_id=original.id, **measurements)
    db.add(remake); db.commit(); db.refresh(remake)
    return RedirectResponse(f"/orders/{remake.id}", status_code=303)

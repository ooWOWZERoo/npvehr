"""Store Operations, Orders, Claim Management, and Catalog sidebar sections
(competitive-review addition). Daily Closing is real/functional; everything
else here is an intentionally polished placeholder reserving the information
architecture -- see the report for what's built vs. stubbed.
"""
from datetime import date, datetime
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from ehr.models.database import get_db, DailyClosing
from ehr.env_info import EHR_ENV
from ehr.auth.permissions import require_role, STORE_OPS_VIEW, STORE_OPS_EDIT, CLAIMS_VIEW, CATALOG_ORDERS_VIEW, ROLE_LABELS
from ehr.auth import csrf

router = APIRouter(tags=["store-ops"])
templates = Jinja2Templates(directory="ehr/templates")
templates.env.globals["ehr_env"] = EHR_ENV
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS

# (code, display label) -- matches Encompass's payment-type set for Daily Closing.
PAYMENT_TYPES = [
    ("cash", "Cash"),
    ("check", "Check"),
    ("credit_card", "Credit Card"),
    ("atm_debit", "ATM/Debit"),
    ("carecredit", "CareCredit"),
    ("amex", "American Express"),
]


def _placeholder(request: Request, title, icon, description, bullets):
    return templates.TemplateResponse(request, "placeholder.html",
        {"title": title, "icon": icon, "description": description, "bullets": bullets})


# --------------------------------------------------------------------------
# Store Operations
# --------------------------------------------------------------------------

@router.get("/store-ops/daily-closing", response_class=HTMLResponse, dependencies=[Depends(require_role(*STORE_OPS_VIEW))])
def daily_closing(request: Request, posting_date: str = None, db: Session = Depends(get_db)):
    today_iso = date.today().isoformat()
    posting_date = posting_date or today_iso
    rows = (db.query(DailyClosing).filter(DailyClosing.posting_date == posting_date)
            .order_by(DailyClosing.payment_type).all())
    by_type = {r.payment_type: r for r in rows}
    history_dates = sorted({r.posting_date for r in db.query(DailyClosing).all()}, reverse=True)[:10]
    history = []
    for d in history_dates:
        d_rows = (db.query(DailyClosing).filter(DailyClosing.posting_date == d)
                  .order_by(DailyClosing.payment_type).all())
        history.append({"posting_date": d, "rows": d_rows,
                         "total_variance": sum((r.variance or 0) for r in d_rows)})
    return templates.TemplateResponse(request, "store_ops/daily_closing.html", {
        "payment_types": PAYMENT_TYPES, "payment_labels": dict(PAYMENT_TYPES), "by_type": by_type,
        "posting_date": posting_date, "today_iso": today_iso, "history": history,
    })


@router.post("/store-ops/daily-closing", dependencies=[Depends(require_role(*STORE_OPS_EDIT))])
async def save_daily_closing(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    csrf.verify_or_403(request.state.csrf_token, form.get("csrf_token"))
    posting_date = form.get("posting_date") or date.today().isoformat()
    now = datetime.utcnow()
    for code, _label in PAYMENT_TYPES:
        try:
            actual = float(form.get(f"actual_{code}") or 0)
        except ValueError:
            actual = 0.0
        calculated = 0.0  # placeholder pending a real transaction ledger -- see model docstring
        explanation = form.get(f"explanation_{code}") or ""
        db.add(DailyClosing(posting_date=posting_date, payment_type=code, calculated_amount=calculated,
            actual_amount=actual, variance=actual - calculated, explanation=explanation, created_at=now))
    db.commit()
    return RedirectResponse(f"/store-ops/daily-closing?posting_date={posting_date}", status_code=303)


@router.get("/store-ops/change-payments", response_class=HTMLResponse, dependencies=[Depends(require_role(*STORE_OPS_VIEW))])
def change_payments(request: Request):
    return _placeholder(request, "Change Payments", "&#128179;",
        "Change Payments will let administrators configure which payment methods this practice "
        "accepts at checkout and Daily Closing, and manage settings specific to each one (e.g. surcharge "
        "rules, processor account details, or disabling a payment type entirely).",
        ["Enable or disable individual payment types (Cash, Check, Credit Card, ATM/Debit, CareCredit, American Express, etc.).",
         "Configure processor/merchant account details per payment type.",
         "Set default display order for the checkout and Daily Closing screens."])


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------

@router.get("/orders/", response_class=HTMLResponse, dependencies=[Depends(require_role(*CATALOG_ORDERS_VIEW))])
def order_management(request: Request):
    return _placeholder(request, "Order Management", "&#128230;",
        "Order Management will track optical lab orders -- frames and lenses sent out for a patient's "
        "glasses -- from the moment they're placed with a lab through fabrication and arrival in-office, "
        "so staff always know the status of a patient's order without calling the lab.",
        ["Create and track frame and lens orders against a specific patient and Rx.",
         "Lab selection, order status (placed, in fabrication, shipped, received, dispensed), and ETA tracking.",
         "Notifications when an order is ready for patient pickup.",
         "Reorder/remake tracking for warranty and redo jobs."])


# --------------------------------------------------------------------------
# Claim Management
# --------------------------------------------------------------------------

CLAIM_SECTIONS = {
    "search": ("Claim Search", "&#128269;",
        "Claim Search will let staff find claims across the whole practice by patient, claim number, "
        "order number, carrier, plan, status, or service date, and act on results in bulk -- mark billed, "
        "ready-to-bill, or on hold -- without opening each claim individually.",
        ["Multi-field search: patient, insured ID/SSN, claim/order number, authorization number, service date range.",
         "Filter by claim status, carrier, plan, and office/location.",
         "A results grid with insurance/patient receivable and paid-amount columns, and bulk status actions.",
         "A standing \"failed claims\" alert so submission errors are never silently missed."]),
    "billing": ("Billing Claims", "&#128203;",
        "Billing Claims will handle claim generation and the claim lifecycle -- turning a completed exam or "
        "billed service into a claim, tracking it line-by-line through adjudication, and producing the "
        "paperwork (CMS-1500 forms) or electronic transmission (EDI) a payer requires.",
        ["Generate claims from completed eye exams and billed services, with CPT/procedure-code line items.",
         "A claim detail view: charge/allowed/copay/paid/outstanding per line item, plus a full claim history log.",
         "Ready-to-Bill-Patient, Ready-to-Bill-Carrier, and Write-Off actions per claim.",
         "CMS-1500 form generation and EDI (electronic) transmission to payers, filterable by carrier/provider/service date."]),
    "payments": ("Process Payments", "&#128179;",
        "Process Payments will record money coming in against claims and patients -- carrier remittances, "
        "patient payments at checkout, and batch adjustments (write-offs, contractual adjustments) applied "
        "across many claims at once by carrier and status.",
        ["Carrier Payments: search/post insurance remittances by carrier, payment type, date, or reference number.",
         "Patient Payments: search/post patient payments by claim number, name, payment type, or date.",
         "Batch Adjustments: select claims by carrier/status/service-date, then apply an adjustment reason across all of them at once.",
         "An outstanding-amount filter so staff can focus on unpaid/underpaid claims first."]),
    "reports": ("Billing Reports", "&#128202;",
        "Billing Reports will cover both fixed operational reports (aging, collections, adjustments) that "
        "staff run regularly, and a dashboard-style analytics view for exploring trends over time.",
        ["Standard reports: Aged Claims, Billing Outstanding Balance, Billing Adjustments, Claim Collection, "
         "Monthly Aged Balancing, On Hold Claims, Patient Refunds, and similar fixed operational reports.",
         "Analytics & Insights dashboards: Accounts Receivable, Net Collections, Production, and Sales Revenue, "
         "grouped by provider/staff and by office.",
         "CSV export of underlying data for reports and dashboards."]),
    "statements": ("Batch Patient Statements", "&#128233;",
        "Batch Patient Statements will generate and send patient billing statements in bulk -- filtered by "
        "office, claim status, carrier, and service date -- rather than staff creating and mailing them one "
        "at a time.",
        ["Filter which patients receive a statement by office, claim status, carrier, and service-date range.",
         "Track whether a patient has already been notified for a given balance, to avoid duplicate statements.",
         "Batch generation and send (mail, email, or portal) rather than one-by-one."]),
}

@router.get("/claims/", response_class=HTMLResponse, dependencies=[Depends(require_role(*CLAIMS_VIEW))])
def claim_management(request: Request):
    title, icon, description, bullets = CLAIM_SECTIONS["search"]
    return _placeholder(request, title, icon, description, bullets)

@router.get("/claims/{section}", response_class=HTMLResponse, dependencies=[Depends(require_role(*CLAIMS_VIEW))])
def claim_section(request: Request, section: str):
    info = CLAIM_SECTIONS.get(section)
    if not info:
        return HTMLResponse("Not found", status_code=404)
    title, icon, description, bullets = info
    return _placeholder(request, title, icon, description, bullets)


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------

CATALOG_SECTIONS = {
    "frames": ("Frames", "&#128083;",
        "The Frames catalog will manage frame inventory as sellable SKUs -- brand, style, color, size, "
        "cost, and retail price -- so staff can look up availability and ring up dispenses at checkout.",
        ["Brand, collection, and style/model tracking.", "Size (eye/bridge/temple), color, and material.",
         "Cost and retail price, with markup rules.", "On-hand quantity by location."]),
    "eyeglass-lenses": ("Eyeglass Lenses", "&#128083;",
        "The Eyeglass Lenses catalog will manage lens products and options -- materials, designs, "
        "coatings, and their pricing -- used when building an optical order.",
        ["Lens material (CR-39, polycarbonate, high-index) and design (single vision, progressive) options.",
         "Coating and treatment add-ons (AR, blue light, photochromic) with pricing.",
         "Vendor/lab-specific product mapping."]),
    "contact-lenses": ("Contact Lenses", "&#128065;",
        "The Contact Lenses catalog will manage contact lens products by brand and parameters (base curve, "
        "diameter, power range), including pricing and rebate/promotion tracking.",
        ["Brand and product line catalog with parameter ranges.", "Unit and box pricing.",
         "Manufacturer rebate and promotion tracking."]),
    "accessories": ("Accessories", "&#128717;&#65039;",
        "The Accessories catalog will manage smaller retail items sold alongside eyewear -- cases, "
        "cleaning supplies, readers, and similar SKUs.",
        ["SKU, description, cost, and retail price.", "On-hand quantity by location.",
         "Simple reorder-point tracking."]),
    "insurance-plans": ("Insurance Plans", "&#127974;",
        "The Insurance Plans catalog will manage payer plan configuration -- which vision and medical "
        "plans this practice accepts, their fee schedules, and coverage rules used when estimating patient "
        "responsibility.",
        ["Payer and plan directory (vision and medical).", "Fee schedules and allowed-amount tables per plan.",
         "Coverage/frequency rules (e.g. exam and materials benefit periods)."]),
}


@router.get("/catalog/{section}", response_class=HTMLResponse, dependencies=[Depends(require_role(*CATALOG_ORDERS_VIEW))])
def catalog_section(request: Request, section: str):
    info = CATALOG_SECTIONS.get(section)
    if not info:
        return HTMLResponse("Not found", status_code=404)
    title, icon, description, bullets = info
    return _placeholder(request, title, icon, description, bullets)

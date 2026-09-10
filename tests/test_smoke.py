"""Smoke tests: the app boots, login works (and rejects bad credentials), and
the main navigation destinations render without error. Not exhaustive
workflow coverage -- just enough to catch a broken build or a routing/auth
regression before it reaches a real deployment.
"""
import re

DEMO_EMAIL = "admin@newpathvision.example"
DEMO_PASSWORD = "ChangeMe123!"


def test_root_redirects_to_login_when_signed_out(live_server, page):
    page.goto(live_server + "/")
    page.wait_for_url(re.compile(r"/login"))
    assert page.locator("h1", has_text="Sign in").is_visible()


def test_invalid_login_shows_error(live_server, page):
    page.goto(live_server + "/login")
    page.fill("#email", DEMO_EMAIL)
    page.fill("#password", "wrong-password")
    page.click("button[type=submit]")
    assert page.locator(".alert-error", has_text="Invalid email or password").is_visible()


def test_valid_login_reaches_dashboard(logged_in_page):
    page = logged_in_page
    assert "New Path Vision EHR" in page.title() or page.locator("text=New Path Vision EHR").first.is_visible()
    # Dashboard-specific content from ehr/app.py's dashboard() route.
    assert page.locator("text=Total Patients").is_visible() or page.locator("text=Patients").first.is_visible()


def test_logout_returns_to_login(logged_in_page, live_server):
    page = logged_in_page
    page.locator("button[type=submit]", has_text="Logout").click()
    page.wait_for_url(re.compile(r"/login"))
    # And a page that requires auth bounces back to login again.
    page.goto(live_server + "/patients/")
    page.wait_for_url(re.compile(r"/login"))


def test_patients_list_loads(logged_in_page, live_server):
    page = logged_in_page
    page.goto(live_server + "/patients/")
    assert page.locator("h1, h2").first.is_visible()
    assert "/login" not in page.url


def test_appointments_calendar_loads(logged_in_page, live_server):
    page = logged_in_page
    page.goto(live_server + "/appointments/calendar")
    assert "/login" not in page.url


def test_new_exam_form_loads(logged_in_page, live_server):
    page = logged_in_page
    page.goto(live_server + "/exams/new")
    assert "/login" not in page.url


def test_visit_focus_toggle_shows_hides_assessment_sections(logged_in_page, live_server):
    """The Visit Focus checkboxes (ehr/templates/exams/form.html) are the one
    behavior curl-based route checks can't confirm -- this is real client-side
    JS, not server-rendered conditionals. Comprehensive/Refractive starts
    checked+visible and Anterior Segment starts unchecked+hidden; toggling
    each checkbox should flip its section's visibility."""
    page = logged_in_page
    page.goto(live_server + "/exams/new")
    refractive_section = page.locator("#focus-refractive")
    anterior_section = page.locator("#focus-anterior")
    assert refractive_section.is_visible()
    assert not anterior_section.is_visible()

    page.locator('.focus-toggle[data-target="focus-anterior"]').check()
    assert anterior_section.is_visible()

    page.locator('.focus-toggle[data-target="focus-refractive"]').uncheck()
    assert not refractive_section.is_visible()


def test_assessment_plan_auto_composed_then_not_overwritten_after_manual_edit(logged_in_page, live_server):
    """The Assessment & Plan composer (ehr/templates/exams/form.html) drafts a
    narrative from the structured chips/dropdowns -- verifies it actually
    composes text, and that it stops overwriting a field once a clinician
    types into it directly (tracked via the textarea's own `input` event,
    which a real keystroke fires but this script's own `.value =` does not)."""
    page = logged_in_page
    page.goto(live_server + "/exams/new")
    assessment = page.locator("#assessment")
    assert assessment.input_value() == ""

    page.locator('input[name="refractive_diagnosis"][value="Myopia"]').check()
    page.locator('select[name="refractive_stability"]').select_option("Stable")
    assert "Myopia" in assessment.input_value()
    assert "stable" in assessment.input_value()

    assessment.fill("Clinician's own wording")
    page.locator('input[name="refractive_diagnosis"][value="Hyperopia"]').check()
    assert assessment.input_value() == "Clinician's own wording"


def test_icd10_suggestion_and_diagnosis_driven_recall_interval(logged_in_page, live_server):
    """The ICD-10 lookup and diagnosis-driven recall interval
    (ehr/templates/exams/form.html) extend the composer -- verifies the
    lookup resolves by (diagnosis, laterality), a second diagnosis appends
    its own code, a glaucoma-suspect secondary finding shortens the
    suggested follow-up to 26 weeks (vs. the 52-week default) and that
    shows up in the composed Plan text, and that manual edits to either
    field are never overwritten (same input-event tracking as assessment/plan)."""
    page = logged_in_page
    page.goto(live_server + "/exams/new")
    dx_codes = page.locator("#diagnosis_codes")
    weeks = page.locator("#follow_up_weeks")
    assert dx_codes.input_value() == ""
    assert weeks.input_value() == ""

    page.locator('input[name="refractive_diagnosis"][value="Myopia"]').check()
    page.locator('select[name="refractive_laterality"]').select_option("OU")
    assert dx_codes.input_value() == "H52.13"
    assert weeks.input_value() == "52"

    page.locator('input[name="refractive_diagnosis"][value="Astigmatism"]').check()
    assert dx_codes.input_value() == "H52.13, H52.203"

    page.locator('input[name="refractive_secondary_findings"][value="Suspect Glaucoma"]').check()
    assert weeks.input_value() == "26"
    assert "26 weeks" in page.locator("#plan").input_value()

    dx_codes.fill("CUSTOM")
    weeks.fill("99")
    page.locator('input[name="refractive_diagnosis"][value="Hyperopia"]').check()
    assert dx_codes.input_value() == "CUSTOM"
    assert weeks.input_value() == "99"


def test_glaucoma_focus_toggle_composer_and_trend_view(logged_in_page, live_server):
    """Posterior Segment / Glaucoma (ehr/templates/exams/form.html,
    ehr/templates/patients/glaucoma_trend_tab.html) -- verifies the third
    Visit Focus chip shows/hides its section like the existing two, the
    composer's glaucoma clauses populate Assessment & Plan, and the new
    patient-workspace trend tab renders a populated chart+table for the
    seeded demo patient (David Wilson, two glaucoma-tracking exams six
    months apart) and an empty state for a patient with none."""
    page = logged_in_page
    page.goto(live_server + "/exams/new")
    glaucoma_section = page.locator("#focus-glaucoma")
    assert not glaucoma_section.is_visible()
    page.locator('.focus-toggle[data-target="focus-glaucoma"]').check()
    assert glaucoma_section.is_visible()

    page.fill('input[name="gt_primary_diagnosis_code"]', "H40.0011")
    page.fill('input[name="gt_iop_current_od"]', "24")
    page.fill('input[name="gt_iop_current_os"]', "25")
    assert "Glaucoma (H40.0011)" in page.locator("#assessment").input_value()
    assert "24/25 mmHg" in page.locator("#assessment").input_value()

    page.locator('input[name="gt_prescribed_glaucoma_meds"][value="Latanoprost 0.005% QHS OU"]').check()
    page.locator('select[name="gt_follow_up_interval"]').select_option("3 months")
    plan = page.locator("#plan").input_value()
    assert "Latanoprost" in plan
    assert "3 months" in plan

    # Patient-workspace trend view: seeded demo patient (David Wilson) has
    # two glaucoma-tracking exams -- populated table + non-empty chart.
    page.goto(live_server + "/patients/")
    page.locator("a", has_text="Wilson").first.click()
    page.locator('a[href$="/glaucoma-trend"]').click()
    assert "glaucoma-trend" in page.url
    assert page.locator("table tbody tr").count() >= 2
    assert page.locator("svg polyline").count() == 2

    # A patient with no glaucoma-tracking history sees the empty state instead.
    page.goto(live_server + "/patients/")
    page.locator("a", has_text="Johnson").first.click()
    page.locator('a[href$="/glaucoma-trend"]').click()
    assert page.locator("text=No glaucoma tracking recorded").is_visible()


def test_binocular_vision_focus_toggle_and_composer(logged_in_page, live_server):
    """Binocular Vision / Pediatrics (ehr/templates/exams/form.html) -- the
    fourth Visit Focus chip shows/hides its section like the existing three,
    and the composer's binocular clauses populate Assessment & Plan from the
    strabismus/home-exercise fields."""
    page = logged_in_page
    page.goto(live_server + "/exams/new")
    binocular_section = page.locator("#focus-binocular")
    assert not binocular_section.is_visible()
    page.locator('.focus-toggle[data-target="focus-binocular"]').check()
    assert binocular_section.is_visible()

    page.fill('input[name="bv_primary_diagnosis_code"]', "H51.11")
    page.locator('select[name="bv_strabismus_present"]').select_option("Yes")
    page.locator('select[name="bv_strabismus_direction"]').select_option("Esotropia")
    assessment = page.locator("#assessment").input_value()
    assert "H51.11" in assessment
    assert "strabismus present (Esotropia)" in assessment

    page.locator('input[name="bv_assigned_home_exercises"][value="Brock String"]').check()
    page.fill('input[name="bv_therapy_session_number"]', "4")
    page.locator('select[name="bv_follow_up_interval"]').select_option("2 weeks")
    plan = page.locator("#plan").input_value()
    assert "Brock String" in plan
    assert "session 4" in plan
    assert "2 weeks" in plan


def test_new_prescription_form_loads(logged_in_page, live_server):
    page = logged_in_page
    page.goto(live_server + "/prescriptions/new")
    assert "/login" not in page.url

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


def test_new_prescription_form_loads(logged_in_page, live_server):
    page = logged_in_page
    page.goto(live_server + "/prescriptions/new")
    assert "/login" not in page.url

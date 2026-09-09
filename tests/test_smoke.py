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


def test_new_prescription_form_loads(logged_in_page, live_server):
    page = logged_in_page
    page.goto(live_server + "/prescriptions/new")
    assert "/login" not in page.url

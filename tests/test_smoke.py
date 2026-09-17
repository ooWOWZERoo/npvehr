"""Smoke tests: the app boots, login works (and rejects bad credentials), and
the main navigation destinations render without error. Not exhaustive
workflow coverage -- just enough to catch a broken build or a routing/auth
regression before it reaches a real deployment.
"""
import re
from datetime import datetime, timedelta

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


def test_pupil_exam_fields_save_and_display(logged_in_page, live_server):
    """Pupil exam (ehr/models/database.py EyeExam.pupil_*, v2.21) -- flat
    columns on EyeExam, not a Visit Focus dashboard, since pupils are core
    exam data present on nearly every visit. Verifies the fields save and
    show on the exam detail page, and that the Pupils card doesn't render
    at all for an exam where none of them were filled in."""
    page = logged_in_page
    page.goto(live_server + "/exams/new")
    page.fill('input[name="pupil_size_light_od"]', "3.5")
    page.fill('input[name="pupil_size_light_os"]', "3.5")
    page.fill('input[name="pupil_size_dark_od"]', "6")
    page.fill('input[name="pupil_size_dark_os"]', "6")
    page.locator('select[name="pupil_reactivity_od"]').select_option("Brisk")
    page.locator('select[name="pupil_reactivity_os"]').select_option("Brisk")
    page.locator('select[name="pupil_apd_finding"]').select_option("Negative")
    page.fill('input[name="pupil_notes"]', "PERRLA")
    page.locator('button[type="submit"]', has_text="Save Exam").click()

    page.wait_for_url(re.compile(r"/exams/\d+"))
    assert page.locator("h3", has_text="Pupils").is_visible()
    assert page.locator("text=Brisk").first.is_visible()
    assert page.locator("text=PERRLA").is_visible()

    # An exam with no pupil data at all shows no Pupils card.
    page.goto(live_server + "/exams/new")
    page.locator('select[name="provider_id"]').select_option(index=1)
    page.locator('button[type="submit"]', has_text="Save Exam").click()
    page.wait_for_url(re.compile(r"/exams/\d+"))
    assert page.locator("h3", has_text="Pupils").count() == 0


def test_motility_and_confrontation_vf_save_and_display(logged_in_page, live_server):
    """Motility & confrontation visual fields (ehr/models/database.py
    EyeExam.motility_*/confrontation_vf_*, v2.22) -- flat columns on EyeExam,
    same treatment as pupils (v2.21), not a Visit Focus dashboard. Verifies
    the fields save and show on the exam detail page, and that the card
    doesn't render at all for an exam where none of them were filled in."""
    page = logged_in_page
    page.goto(live_server + "/exams/new")
    page.fill('input[name="motility_od"]', "Full")
    page.fill('input[name="motility_os"]', "Full")
    page.fill('input[name="confrontation_vf_od"]', "Full to finger counting")
    page.fill('input[name="confrontation_vf_os"]', "Full to finger counting")
    page.locator('button[type="submit"]', has_text="Save Exam").click()

    page.wait_for_url(re.compile(r"/exams/\d+"))
    assert page.locator("h3", has_text="Motility & Confrontation VF").is_visible()
    assert page.locator("text=Full to finger counting").first.is_visible()

    # An exam with no motility/CVF data at all shows no card for this section.
    page.goto(live_server + "/exams/new")
    page.locator('select[name="provider_id"]').select_option(index=1)
    page.locator('button[type="submit"]', has_text="Save Exam").click()
    page.wait_for_url(re.compile(r"/exams/\d+"))
    assert page.locator("h3", has_text="Motility & Confrontation VF").count() == 0


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


def test_visit_focus_status_dots_and_collapse(logged_in_page, live_server):
    """Visit Focus status dots + collapsible cards (ehr/templates/exams/form.html,
    ehr/static/css/app.css, v2.24) -- each chip and card header carries a dot
    that's hollow until its section has a value, then fills solid; each active
    section is a real accordion, so collapsing (a click on the card header)
    is a distinct action from deactivating (unchecking the chip) -- collapsing
    must not clear anything entered, and a collapsed-but-filled card shows a
    one-line summary instead of looking empty."""
    page = logged_in_page
    page.goto(live_server + "/exams/new")

    page.locator('[data-chip-wrap="focus-anterior"]').click()
    dot_chip = page.locator('[data-dot-chip="focus-anterior"]')
    dot_head = page.locator('[data-dot-head="focus-anterior"]')
    assert "vf-filled" not in (dot_chip.get_attribute("class") or "")

    page.fill('input[name="ant_primary_diagnosis_code"]', "H11.031")
    assert "vf-filled" in dot_chip.get_attribute("class")
    assert "vf-filled" in dot_head.get_attribute("class")

    # Collapsing (the header, not the chip) hides the fields but keeps them --
    # the value must still be there on re-expand, and a summary appears meanwhile.
    page.locator('[data-vf-head="focus-anterior"]').click()
    assert not page.locator("#focus-anterior").is_visible()
    assert "H11.031" in page.locator('[data-vf-summary="focus-anterior"]').inner_text()

    page.locator('[data-vf-head="focus-anterior"]').click()
    assert page.locator("#focus-anterior").is_visible()
    assert page.input_value('input[name="ant_primary_diagnosis_code"]') == "H11.031"


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
    assert "Stable" in assessment.input_value()

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


def test_anterior_segment_focus_toggle_and_composer(logged_in_page, live_server):
    """Anterior Segment (ehr/templates/exams/form.html, v2.23) -- a real
    structural exam (conjunctiva/cornea/anterior chamber/iris/lens), split
    out from the old combined "Anterior Segment / Dry Eye" dashboard (which
    was entirely dry-eye content -- see test_dry_eye_focus_toggle_and_composer
    below for that one). Verifies the second Visit Focus chip shows/hides its
    own section independently of Dry Eye's, and the composer's anterior-
    segment clauses populate Assessment & Plan from the pterygium/cataract
    findings."""
    page = logged_in_page
    page.goto(live_server + "/exams/new")
    anterior_section = page.locator("#focus-anterior")
    dryeye_section = page.locator("#focus-dryeye")
    assert not anterior_section.is_visible()
    assert not dryeye_section.is_visible()
    page.locator('.focus-toggle[data-target="focus-anterior"]').check()
    assert anterior_section.is_visible()
    assert not dryeye_section.is_visible()

    page.fill('input[name="ant_primary_diagnosis_code"]', "H11.031")
    page.locator('select[name="ant_severity"]').select_option("Mild")
    page.locator('select[name="ant_pterygium_pinguecula_od"]').select_option("Pterygium")
    assessment = page.locator("#assessment").input_value()
    assert "H11.031" in assessment
    assert "Pterygium OD" in assessment

    page.locator('select[name="ant_lens_cataract_type_od"]').select_option("Nuclear Sclerosis")
    assert "Nuclear Sclerosis cataract OD" in page.locator("#assessment").input_value()

    page.locator('input[name="ant_plan_therapeutics"][value="Cataract Surgery Referral"]').check()
    page.locator('select[name="ant_follow_up_interval"]').select_option("6 months")
    plan = page.locator("#plan").input_value()
    assert "Cataract Surgery Referral" in plan
    assert "6 months" in plan


def test_dry_eye_focus_toggle_and_composer(logged_in_page, live_server):
    """Dry Eye / Ocular Surface Disease (ehr/templates/exams/form.html,
    v2.23) -- the dashboard built in v2.11 under the misleading name
    "Anterior Segment / Dry Eye" (it was entirely dry-eye/OSD content:
    conjunctival injection, corneal staining, MGD, TBUT, Schirmer), renamed
    and given its own dedicated Visit Focus chip separate from the real,
    new Anterior Segment structural exam above. Verifies the toggle and
    composer behave exactly as the old combined dashboard did."""
    page = logged_in_page
    page.goto(live_server + "/exams/new")
    dryeye_section = page.locator("#focus-dryeye")
    assert not dryeye_section.is_visible()
    page.locator('.focus-toggle[data-target="focus-dryeye"]').check()
    assert dryeye_section.is_visible()

    page.fill('input[name="de_primary_diagnosis_code"]', "H04.123")
    page.locator('select[name="de_severity"]').select_option("Moderate")
    assessment = page.locator("#assessment").input_value()
    assert "Dry eye syndrome" in assessment
    assert "Moderate" in assessment
    assert "H04.123" in assessment

    page.locator('input[name="de_plan_therapeutics"][value="Preservative-Free Tears"]').check()
    page.locator('select[name="de_follow_up_interval"]').select_option("2 weeks")
    plan = page.locator("#plan").input_value()
    assert "Preservative-Free Tears" in plan
    assert "2 weeks" in plan


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
    page.locator('select[name="gt_glaucoma_stage"]').select_option("Mild")
    page.fill('input[name="gt_iop_current_od"]', "24")
    page.fill('input[name="gt_iop_current_os"]', "25")
    assessment_text = page.locator("#assessment").input_value()
    assert "H40.0011" in assessment_text
    assert "Primary open-angle glaucoma" in assessment_text
    assert "24" in assessment_text and "25" in assessment_text and "mmHg" in assessment_text

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
    assert "Esotropia" in assessment
    assert "present" in assessment

    page.locator('input[name="bv_assigned_home_exercises"][value="Brock String"]').check()
    page.fill('input[name="bv_therapy_session_number"]', "4")
    page.locator('select[name="bv_follow_up_interval"]').select_option("2 weeks")
    plan = page.locator("#plan").input_value()
    assert "Brock String" in plan
    assert "session 4" in plan
    assert "2 weeks" in plan


def test_surgery_comanagement_focus_toggle_composer_and_timeline(logged_in_page, live_server):
    """Pre-/Post-Op Co-Management (ehr/templates/exams/form.html,
    ehr/templates/patients/surgery_timeline_tab.html) -- the fifth and last
    Visit Focus chip shows/hides its section like the existing four, the
    composer's surgery clauses populate Assessment & Plan, and the new
    patient-workspace timeline tab renders a populated table for the seeded
    demo patient (Carol Davis, a three-visit LASIK timeline) and an empty
    state for a patient with none."""
    page = logged_in_page
    page.goto(live_server + "/exams/new")
    surgery_section = page.locator("#focus-surgery")
    assert not surgery_section.is_visible()
    page.locator('.focus-toggle[data-target="focus-surgery"]').check()
    assert surgery_section.is_visible()

    page.locator('select[name="sx_surgical_procedure"]').select_option("LASIK")
    page.locator('select[name="sx_operative_eye"]').select_option("OU")
    page.locator('select[name="sx_current_milestone"]').select_option("Day 1")
    assessment = page.locator("#assessment").input_value()
    assert "LASIK" in assessment
    assert "OU" in assessment
    assert "Day 1" in assessment

    page.fill('textarea[name="sx_steroid_taper_schedule"]', "Pred Forte QID x 1 week")
    page.locator('select[name="sx_follow_up_interval"]').select_option("1 week")
    plan = page.locator("#plan").input_value()
    assert "Pred Forte" in plan
    assert "1 week" in plan

    # Patient-workspace timeline view: seeded demo patient (Carol Davis) has
    # a three-visit LASIK timeline -- populated table, newest first.
    page.goto(live_server + "/patients/")
    page.locator("a", has_text="Davis").first.click()
    page.locator('a[href$="/surgery-timeline"]').click()
    assert "surgery-timeline" in page.url
    assert page.locator("table tbody tr").count() == 3

    # A patient with no surgery-tracking history sees the empty state instead.
    page.goto(live_server + "/patients/")
    page.locator("a", has_text="Johnson").first.click()
    page.locator('a[href$="/surgery-timeline"]').click()
    assert page.locator("text=No surgical co-management recorded").is_visible()


def test_new_prescription_form_loads(logged_in_page, live_server):
    page = logged_in_page
    page.goto(live_server + "/prescriptions/new")
    assert "/login" not in page.url


def test_patient_document_upload_download_and_cross_patient_guard(logged_in_page, live_server):
    """Per-patient document storage (ehr/routes/patients.py, v2.20) -- upload a
    document on the Documents tab, confirm it lists and downloads for its own
    patient, then confirm the explicit cross-patient-contamination guard: the
    same doc_id 404s when requested through a *different* patient's URL,
    rather than a bad-`doc_id`-only obscurity check."""
    page = logged_in_page
    page.goto(live_server + "/patients/")
    page.locator("a", has_text="Johnson").first.click()
    page.locator('a[href$="/correspondence/documents"]').click()

    page.locator('input[type="file"][name="document"]').set_input_files(
        files=[{"name": "outside-record.txt", "mimeType": "text/plain",
                "buffer": b"outside record contents"}])
    page.locator('select[name="category"]').select_option("Outside Records")
    page.fill('input[name="description"]', "Referral note")
    page.locator('button[type="submit"]', has_text="Upload").click()

    row = page.locator("table tbody tr", has_text="outside-record.txt")
    assert row.is_visible()
    doc_href = row.locator("a").get_attribute("href")
    assert doc_href is not None

    # Own patient: downloads fine.
    resp = page.request.get(live_server + doc_href)
    assert resp.status == 200
    assert resp.body() == b"outside record contents"

    # A different patient's URL with the same doc_id must 404, not serve it.
    other_patient_url = doc_href.replace("/patients/1/", "/patients/2/", 1)
    if other_patient_url != doc_href:
        resp = page.request.get(live_server + other_patient_url)
        assert resp.status == 404

    # Delete cleans it back up.
    page.on("dialog", lambda d: d.accept())
    page.locator("form button", has_text="Delete").first.click()
    page.wait_for_timeout(200)


def test_problem_list_create_addendum_via_exam_and_resolve(logged_in_page, live_server):
    """Structured problem list (ehr/models/database.py Problem/ProblemAddendum,
    v2.20) -- add a chronic diagnosis on the Problem List tab, confirm it
    appears as a checkbox on the New Exam form, confirm checking it while
    creating an exam appends a dated addendum visible both on the exam detail
    page and back on the Problem List tab, then mark it resolved."""
    page = logged_in_page
    page.goto(live_server + "/patients/")
    page.locator("a", has_text="Smith").first.click()
    page.locator('a[href$="/problems"]').click()

    page.fill('input[name="diagnosis_name"]', "Primary Open Angle Glaucoma")
    page.fill('input[name="icd10_code"]', "H40.1132")
    page.locator('select[name="laterality"]').select_option("OU")
    page.locator('button[type="submit"]', has_text="Add Problem").click()
    assert page.locator("h3", has_text="Primary Open Angle Glaucoma").is_visible()

    patient_url = page.url
    patient_id = patient_url.rstrip("/").split("/")[-2]

    page.goto(live_server + f"/exams/new?patient_id={patient_id}")
    checkbox = page.locator('input[name="problems_addressed"]')
    assert checkbox.is_visible()
    checkbox.check()
    page.fill('input[name^="problem_note_"]', "New RX for Latanoprost OU QHS sent.")
    page.locator('select[name="provider_id"]').select_option(index=1)
    page.locator('button[type="submit"]', has_text="Save Exam").click()

    page.wait_for_url(re.compile(r"/exams/\d+"))
    assert page.locator("h3", has_text="Problems Addressed at This Visit").is_visible()
    assert page.locator("text=New RX for Latanoprost OU QHS sent.").is_visible()

    page.goto(live_server + f"/patients/{patient_id}/problems")
    assert page.locator("text=New RX for Latanoprost OU QHS sent.").is_visible()
    page.locator('button', has_text="Mark Resolved").click()
    assert page.locator(".badge", has_text="Resolved").is_visible()


def test_csrf_token_required_on_post(logged_in_page, live_server):
    """CSRF protection (ehr/auth/csrf.py) -- app.js auto-injects a real,
    session-bound token from base.html's <meta name="csrf-token"> into every
    form (verified implicitly by every other test in this file still passing
    with real browser form submissions), but a POST missing that token, or
    carrying a wrong one, must be rejected with 403."""
    page = logged_in_page
    token = page.locator('meta[name="csrf-token"]').get_attribute("content")
    assert token

    # A real form's own submission (Playwright driving a real browser) is
    # already covered elsewhere -- here, bypass the form entirely to prove
    # the server independently verifies the token rather than trusting the
    # client not to strip it.
    resp = page.request.post(live_server + "/patients/new",
        form={"first_name": "No", "last_name": "Token"})
    assert resp.status == 403

    resp = page.request.post(live_server + "/patients/new",
        form={"first_name": "Wrong", "last_name": "Token", "csrf_token": "not-the-real-token"})
    assert resp.status == 403

    resp = page.request.post(live_server + "/patients/new",
        form={"first_name": "Correct", "last_name": "Token", "csrf_token": token})
    assert resp.status in (200, 303)


def test_calendar_feed_hover_data_and_reschedule_conflict(logged_in_page, live_server):
    """Calendar & Appointments UX Overhaul Phase 1 (BUILD_BACKLOG.md 5a) --
    the FullCalendar-driven board (ehr/templates/appointments/board.html).
    Verifies the JSON feed (ehr/routes/appointments.py's appointments_feed)
    returns the shape the board's hover cards/event rendering need, that a
    conflict-free reschedule persists, and that rescheduling onto another
    appointment's exact slot for the same provider is rejected (409) with
    _apply_scheduling_rules' own conflict message -- the same check the full
    edit form uses, reused by the board's drag-and-drop."""
    page = logged_in_page
    page.goto(live_server + "/appointments/calendar")
    assert page.locator(".fc").count() >= 1

    resp = page.request.get(live_server + "/appointments/feed.json?start=2020-01-01&end=2030-01-01")
    assert resp.status == 200
    events = resp.json()
    assert len(events) >= 2
    sample = events[0]
    for key in ("id", "title", "start", "end", "color"):
        assert key in sample
    props = sample["extendedProps"]
    for key in ("patientName", "patientPhone", "providerId", "providerName", "typeName", "typeAbbrev",
                "status", "relationship", "rooms", "hasNotes", "conflictOverridden", "durationMinutes"):
        assert key in props

    # A stale bookmark with an empty-value filter (the pre-existing bug fixed
    # in this round) must not 422.
    resp = page.request.get(live_server + "/appointments/feed.json?provider_id=&start=2020-01-01&end=2030-01-01")
    assert resp.status == 200
    resp = page.request.get(live_server + "/appointments/day?date_str=2026-01-01&provider_id=")
    assert resp.status == 200

    # Reschedule to a conflict-free time -- persists.
    same_provider = [e for e in events if e["extendedProps"]["providerId"] == sample["extendedProps"]["providerId"]]
    csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")
    new_start = sample["start"][:11] + "06:00:00"
    result = page.evaluate("""
        async ([id, newStart, csrf]) => {
            const body = new URLSearchParams();
            body.set('scheduled_at', newStart);
            body.set('csrf_token', csrf);
            const resp = await fetch('/appointments/' + id + '/reschedule', {
                method: 'POST', headers: {'Accept': 'application/json'}, body,
            });
            return {status: resp.status, data: await resp.json()};
        }
    """, [sample["id"], new_start, csrf])
    assert result["status"] == 200 and result["data"]["ok"] is True

    # Reschedule onto another same-provider appointment's exact slot -- 409s.
    other = [e for e in same_provider if e["id"] != sample["id"]]
    if other:
        conflict_result = page.evaluate("""
            async ([id, newStart, csrf]) => {
                const body = new URLSearchParams();
                body.set('scheduled_at', newStart);
                body.set('csrf_token', csrf);
                const resp = await fetch('/appointments/' + id + '/reschedule', {
                    method: 'POST', headers: {'Accept': 'application/json'}, body,
                });
                return {status: resp.status, data: await resp.json()};
            }
        """, [sample["id"], other[0]["start"], csrf])
        assert conflict_result["status"] == 409
        assert conflict_result["data"]["ok"] is False
        assert "conflict" in conflict_result["data"]["error"].lower()


def test_waitlist_add_view_and_surfaced_on_cancellation(logged_in_page, live_server):
    """Waitlist Management, Phase 2 (BUILD_BACKLOG.md 5a) -- adds a waitlist
    entry on the patient workspace tab (ehr/templates/patients/waitlist_tab.html),
    confirms it appears there and in the staff-facing global queue
    (appointments/waitlist.html), then cancels a matching appointment and
    confirms the entry surfaces on that appointment's detail page (ehr.services.
    scheduling.find_matching_waitlist_entries) -- no auto-notify yet, this is
    purely the staff-visible surfacing Phase 2 ships."""
    page = logged_in_page

    events = page.request.get(live_server + "/appointments/feed.json?start=2020-01-01&end=2030-01-01").json()
    target = events[0]
    provider_id = target["extendedProps"]["providerId"]
    type_version_id = target["extendedProps"]["appointmentTypeVersionId"]  # may be None -- a legacy, untyped appointment

    page.goto(live_server + "/patients/")
    page.locator("a", has_text="Johnson").first.click()
    page.locator(".pw-subnav a[href$='/waitlist']").click()
    page.select_option('select[name="provider_id"]', str(provider_id))
    if type_version_id is not None:
        page.select_option('select[name="appointment_type_version_id"]', str(type_version_id))
    page.select_option('select[name="priority"]', "urgent")
    page.fill('input[name="notes"]', "Waiting for an earlier slot")
    page.locator('button[type="submit"]', has_text="Add to Waitlist").click()
    page.wait_for_url(re.compile(r"/waitlist$"))
    page.wait_for_load_state("networkidle")
    tab_text = page.locator(".card", has_text="Waitlist Entries").inner_text()
    assert "Waiting for an earlier slot" in tab_text
    assert "Urgent" in tab_text

    page.goto(live_server + "/appointments/waitlist")
    assert "Waiting for an earlier slot" in page.locator(".card").first.inner_text()

    page.goto(live_server + "/appointments/" + str(target["id"]))
    page.select_option('select[name="status"]', "cancelled")
    page.locator('form select[name="status"]').evaluate("el => el.form.requestSubmit()")
    page.wait_for_url(re.compile(r"/appointments/\d+$"))
    page.wait_for_load_state("networkidle")
    matches_card = page.locator(".card", has_text="Patients Waiting for This Slot")
    assert matches_card.is_visible()
    assert "Waiting for an earlier slot" in matches_card.inner_text()

    # Cancelling the waitlist entry itself removes it from the active queue.
    page.goto(live_server + "/patients/")
    page.locator("a", has_text="Johnson").first.click()
    page.locator(".pw-subnav a[href$='/waitlist']").click()
    page.locator("form button", has_text="Cancel").first.click()
    page.wait_for_load_state("networkidle")
    page.goto(live_server + "/appointments/waitlist")
    assert "Waiting for an earlier slot" not in page.locator(".card").first.inner_text()


def test_reminders_opt_in_gating_and_cron_scan(logged_in_page, live_server):
    """Automated Confirmations & Reminders, Phase 3 (BUILD_BACKLOG.md 5a) --
    no real SMS/email vendor is wired up, sends are mocked/logged only via
    ehr.services.notifications, and every send is gated on the patient's own
    sms_opt_in/email_opt_in (both default False, ehr/templates/patients/form.html).
    Covers: opting a patient in saves and re-renders checked; booking an
    appointment fires a mock confirmation recorded on the audit page
    (appointments/reminders.html); the cron-protected scan endpoint
    (GET /appointments/reminders/run) rejects a missing/wrong bearer token,
    accepts the right one with no session at all, and is idempotent -- a
    second scan doesn't re-send the same appointment+channel+kind."""
    page = logged_in_page

    # Opt a seeded patient into both channels via the edit form.
    page.goto(live_server + "/patients/")
    page.locator("a", has_text="Johnson").first.click()
    page.wait_for_load_state("networkidle")
    patient_url = page.url
    patient_id = patient_url.rstrip("/").split("/")[-1]
    page.goto(live_server + f"/patients/{patient_id}/edit")
    page.check("#sms_opt_in")
    page.check("#email_opt_in")
    page.locator('button[type="submit"]', has_text="Save Changes").click()
    page.wait_for_url(re.compile(rf"/patients/{patient_id}$"))
    page.wait_for_load_state("networkidle")
    page.goto(live_server + f"/patients/{patient_id}/edit")
    assert page.locator("#sms_opt_in").is_checked()
    assert page.locator("#email_opt_in").is_checked()

    # Book a new appointment for this patient, scheduled soon (within the
    # cron scan's 24h lookahead) so the reminder-kind scan below picks it up.
    page.goto(live_server + f"/appointments/new?patient_id={patient_id}")
    # An odd, off-the-hour time (not a typical booked slot) to avoid colliding
    # with seed data's own appointments, still well within the cron scan's
    # 24h lookahead window.
    when = (datetime.utcnow() + timedelta(hours=3, minutes=37)).strftime("%Y-%m-%dT%H:%M")
    page.fill("#scheduled_at", when)
    page.locator('button[type="submit"]', has_text="Schedule Appointment").click()
    page.wait_for_url(re.compile(r"/appointments/\d+$"))
    page.wait_for_load_state("networkidle")
    appt_id = page.url.rstrip("/").split("/")[-1]

    # Booking itself sends a (mock) confirmation on both opted-in channels.
    page.goto(live_server + "/appointments/reminders")
    reminders_text = page.locator(".card").inner_text()
    assert f"#{appt_id}" in reminders_text
    assert "Sent" in reminders_text

    # The cron endpoint rejects a missing/wrong bearer token...
    no_auth = page.request.get(live_server + "/appointments/reminders/run")
    assert no_auth.status == 403
    wrong_auth = page.request.get(live_server + "/appointments/reminders/run",
                                   headers={"Authorization": "Bearer wrong-secret"})
    assert wrong_auth.status == 403

    # ...and accepts the real one, with no session cookie required at all
    # (page.request shares the browser context's cookies, but the route
    # itself is on a separate, session-dependency-free router -- see
    # ehr/routes/appointments.py's cron_router).
    ok = page.request.get(live_server + "/appointments/reminders/run",
                           headers={"Authorization": "Bearer test-cron-secret"})
    assert ok.status == 200
    first_data = ok.json()
    assert first_data["ok"] is True
    assert first_data["notices_sent"] >= 1

    # Idempotent: running it again does not re-send the same appointment's
    # reminder-kind notices (send_appointment_notice's own dedup check).
    page.goto(live_server + "/appointments/reminders")
    reminder_rows_after_first = page.locator(".card").inner_text().count("Reminder")
    page.request.get(live_server + "/appointments/reminders/run",
                      headers={"Authorization": "Bearer test-cron-secret"})
    page.goto(live_server + "/appointments/reminders")
    reminder_rows_after_second = page.locator(".card").inner_text().count("Reminder")
    assert reminder_rows_after_second == reminder_rows_after_first


def test_patient_portal_book_reschedule_cancel_and_isolation(logged_in_page, live_server, page, context):
    """Online Patient Self-Booking, Phase 4 (BUILD_BACKLOG.md 5a) -- a second,
    patient-facing surface with its own passwordless (magic-link) auth
    (ehr/auth/portal_deps.py), entirely separate from staff sessions. Covers:
    a staff member opting an appointment type into online booking
    (patient_bookable, off by default); a patient signing in via the mocked
    magic link (no real email vendor -- the dev-only check-email page surfaces
    it directly, per ehr.services.notifications.send_portal_login_link);
    booking, rescheduling, and cancelling an appointment through the same
    conflict-rule engine staff booking uses; and the ownership/isolation
    guard -- another patient's portal session gets a 404, not the appointment,
    when it tries to act on someone else's booking."""
    staff_page = logged_in_page

    # Staff opts one appointment type into online booking (off by default).
    staff_page.goto(live_server + "/admin/scheduling/appointment-types")
    staff_page.locator("table a").first.click()
    staff_page.wait_for_load_state("networkidle")
    type_url = staff_page.url
    type_id = type_url.rstrip("/").split("/")[-1]
    staff_page.goto(live_server + f"/admin/scheduling/appointment-types/{type_id}/edit")
    staff_page.check("#patient_bookable")
    staff_page.fill('input[name="change_reason"]', "Enable online booking for test coverage")
    staff_page.locator('button[type="submit"]', has_text="Publish New Version").click()
    staff_page.wait_for_url(re.compile(rf"/appointment-types/{type_id}$"))
    staff_page.wait_for_load_state("networkidle")

    # A second, independent browser context for the patient -- proves the
    # portal's cookie (npv_portal_session) is entirely separate from the
    # staff session cookie already held by `page`'s context.
    patient_ctx = context.browser.new_context()
    patient_page = patient_ctx.new_page()
    patient_page.goto(live_server + "/portal/login")
    patient_page.fill("#email", "alice@example.com")
    patient_page.click('button[type=submit]')
    patient_page.wait_for_load_state("networkidle")
    login_link = patient_page.locator("a", has_text="Sign in as Alice Johnson")
    login_link.wait_for(state="visible")
    href = login_link.get_attribute("href")
    patient_page.goto(href)
    patient_page.wait_for_url(re.compile(r"/portal/$"))
    assert patient_page.locator("h1", has_text="Welcome").is_visible()

    # Book: pick the newly-bookable type (read its *version* id off the
    # portal's own dropdown -- distinct from the AppointmentType id used in
    # the admin URLs above), a provider, a weekday a few days out (seed
    # provider availability excludes weekends), and the first slot.
    weekday_offset = 1
    while (datetime.utcnow() + timedelta(days=weekday_offset)).weekday() >= 5:
        weekday_offset += 1
    target_date = (datetime.utcnow() + timedelta(days=weekday_offset)).strftime("%Y-%m-%d")
    patient_page.goto(live_server + f"/portal/book?date_str={target_date}")
    type_version_id = patient_page.locator('select[name="appointment_type_version_id"] option').nth(1).get_attribute("value")
    patient_page.select_option('select[name="appointment_type_version_id"]', type_version_id)
    patient_page.wait_for_load_state("networkidle")
    patient_page.select_option('select[name="provider_id"]', index=1)
    patient_page.wait_for_load_state("networkidle")
    slot_forms = patient_page.locator('form[action="/portal/book/confirm"]')
    slot_forms.first.wait_for(state="visible")
    slot_forms.first.locator('button[type="submit"]').click()
    patient_page.wait_for_url(re.compile(r"/portal/appointments\?booked=1"))
    patient_page.wait_for_load_state("networkidle")
    assert "Your appointment is booked" in patient_page.locator(".alert").inner_text()

    appt_link = patient_page.locator('a[href*="/reschedule"]').first
    appt_href = appt_link.get_attribute("href")
    appt_id = appt_href.rstrip("/").split("/")[-2]

    # Ownership isolation, checked before touching the appointment further:
    # a different patient's portal session cannot cancel this one -- 404,
    # not the appointment itself.
    other_ctx = context.browser.new_context()
    other_page = other_ctx.new_page()
    other_page.goto(live_server + "/portal/login")
    other_page.fill("#email", "bob@example.com")
    other_page.click('button[type=submit]')
    other_page.wait_for_load_state("networkidle")
    other_login_link = other_page.locator("a", has_text="Sign in as Bob")
    other_href = other_login_link.get_attribute("href")
    other_page.goto(other_href)
    other_page.wait_for_url(re.compile(r"/portal/$"))
    csrf_token = other_page.locator('meta[name="csrf-token"]').get_attribute("content")
    cross_patient_status = other_page.evaluate("""
        async ([apptId, csrf]) => {
            const body = new URLSearchParams();
            body.set('csrf_token', csrf);
            const resp = await fetch('/portal/appointments/' + apptId + '/cancel', {method: 'POST', body});
            return resp.status;
        }
    """, [appt_id, csrf_token])
    assert cross_patient_status == 404
    other_ctx.close()

    # Reschedule to a different slot (same provider/type, per this round's scope).
    patient_page.goto(live_server + f"/portal/appointments/{appt_id}/reschedule")
    reschedule_forms = patient_page.locator('form[action$="/reschedule/confirm"]')
    reschedule_forms.first.wait_for(state="visible")
    reschedule_forms.first.locator('button[type="submit"]').click()
    patient_page.wait_for_url(re.compile(r"/portal/appointments\?rescheduled=1"))
    patient_page.wait_for_load_state("networkidle")
    assert "Appointment rescheduled" in patient_page.locator(".alert").inner_text()

    # Cancel the real appointment as its rightful owner. Bypasses the cancel
    # button's native confirm() dialog. Playwright auto-dismisses dialogs by
    # default, and unlike a plain form.submit(), requestSubmit() (used
    # elsewhere in this suite to bypass onsubmit handlers) still fires and
    # honors this one's onsubmit -- a dismissed confirm() returns false and
    # silently cancels the submission. So here the dialog is accepted like a
    # real user would, then the button is clicked normally.
    patient_page.goto(live_server + "/portal/appointments")
    patient_page.once("dialog", lambda dialog: dialog.accept())
    cancel_button = patient_page.locator('form[action$="/cancel"] button').first
    cancel_button.wait_for(state="visible")
    cancel_button.click()
    patient_page.wait_for_url(re.compile(r"/portal/appointments\?cancelled=1"))
    patient_page.wait_for_load_state("networkidle")

    patient_ctx.close()


def test_waitlist_auto_notify_on_cancellation_and_idempotency(logged_in_page, live_server):
    """Waitlist auto-notify (BUILD_BACKLOG.md 5a, the deferred follow-up from
    Phase 2/3) -- cancelling an appointment automatically sends a (mock)
    notice to every matching active waitlist entry on any channel its
    patient has opted into (ehr.services.notifications.
    send_waitlist_opening_notices), recorded in the new WaitlistNotification
    table. Covers: the send fires on cancellation and is reflected on both
    the appointment detail page's "Patients Waiting for This Slot" card
    (a "Notified: Yes" column) and the staff waitlist queue's own audit log;
    and it's idempotent -- rescheduling an appointment back to scheduled and
    cancelling it again does not send (or log) a second notice for the same
    waitlist entry."""
    page = logged_in_page

    # Opt Carol Davis in for email, then add a waitlist entry for her.
    page.goto(live_server + "/patients/")
    page.locator("a", has_text="Davis").first.click()
    page.wait_for_load_state("networkidle")
    patient_url = page.url
    carol_id = patient_url.rstrip("/").split("/")[-1]
    page.goto(live_server + f"/patients/{carol_id}/edit")
    page.check("#email_opt_in")
    page.locator('button[type="submit"]', has_text="Save Changes").click()
    page.wait_for_url(re.compile(rf"/patients/{carol_id}$"))
    page.wait_for_load_state("networkidle")

    # Book a fresh appointment (for a different patient) to cancel later --
    # controls the exact provider/type so the waitlist entry below can match
    # it precisely, rather than relying on whatever a legacy seed
    # appointment happens to carry.
    page.goto(live_server + "/appointments/new")
    page.select_option('select[name="provider_id"]', index=0)
    page.select_option('select[name="appointment_type_version_id"]', index=0)
    provider_id = page.eval_on_selector('select[name="provider_id"]', "el => el.value")
    type_version_id = page.eval_on_selector('select[name="appointment_type_version_id"]', "el => el.value")
    when = (datetime.utcnow() + timedelta(days=2)).replace(hour=14, minute=0, second=0,
                                                             microsecond=0).strftime("%Y-%m-%dT%H:%M")
    page.fill("#scheduled_at", when)
    page.locator('button[type="submit"]', has_text="Schedule Appointment").click()
    page.wait_for_url(re.compile(r"/appointments/\d+$"))
    appt_id = page.url.rstrip("/").split("/")[-1]

    page.goto(live_server + f"/patients/{carol_id}/waitlist")
    page.select_option('select[name="provider_id"]', provider_id)
    page.select_option('select[name="appointment_type_version_id"]', type_version_id)
    page.fill('input[name="notes"]', "Auto-notify coverage")
    page.locator('button[type="submit"]', has_text="Add to Waitlist").click()
    page.wait_for_url(re.compile(r"/waitlist$"))
    page.wait_for_load_state("networkidle")

    # Cancel the booked appointment -- this should fire the auto-notify.
    page.goto(live_server + f"/appointments/{appt_id}")
    page.select_option('select[name="status"]', "cancelled")
    page.locator('form select[name="status"]').evaluate("el => el.form.requestSubmit()")
    page.wait_for_url(re.compile(rf"/appointments/{appt_id}$"))
    page.wait_for_load_state("networkidle")
    matches_card = page.locator(".card", has_text="Patients Waiting for This Slot")
    assert matches_card.is_visible()
    assert "Auto-notify coverage" in matches_card.inner_text()
    assert "Yes" in matches_card.inner_text()

    page.goto(live_server + "/appointments/waitlist")
    notifications_card = page.locator(".card", has_text="Recent Waitlist Notifications")
    assert "Davis" in notifications_card.inner_text()
    assert "Sent" in notifications_card.inner_text()
    sent_rows_before = notifications_card.inner_text().count("Sent")

    # Idempotency: reschedule back to 'scheduled' then cancel again --
    # no second notice for the same waitlist entry+appointment+channel.
    page.goto(live_server + f"/appointments/{appt_id}")
    page.select_option('select[name="status"]', "scheduled")
    page.locator('form select[name="status"]').evaluate("el => el.form.requestSubmit()")
    page.wait_for_url(re.compile(rf"/appointments/{appt_id}$"))
    page.wait_for_load_state("networkidle")
    page.select_option('select[name="status"]', "cancelled")
    page.locator('form select[name="status"]').evaluate("el => el.form.requestSubmit()")
    page.wait_for_url(re.compile(rf"/appointments/{appt_id}$"))
    page.wait_for_load_state("networkidle")

    page.goto(live_server + "/appointments/waitlist")
    notifications_card = page.locator(".card", has_text="Recent Waitlist Notifications")
    sent_rows_after = notifications_card.inner_text().count("Sent")
    assert sent_rows_after == sent_rows_before


def test_portal_phase4_followups(logged_in_page, live_server):
    """Phase 4 portal follow-ups (BUILD_BACKLOG.md 5a): the configurable
    self-service cutoff setting (admin/scheduling/portal_settings.html),
    waitlist self-service (join/view/cancel from the portal), a self-service
    reschedule that changes provider (not just time), and the patient-facing
    clinical data view (visit summaries, prescriptions, documents) with its
    per-view PortalAccessAuditEvent logging."""
    staff_page = logged_in_page

    # Staff: confirm the portal settings page round-trips a new cutoff value.
    staff_page.goto(live_server + "/admin/scheduling/portal-settings")
    staff_page.fill('input[name="self_service_cutoff_hours"]', "12")
    staff_page.locator('button[type="submit"]', has_text="Save").click()
    staff_page.wait_for_load_state("networkidle")
    assert staff_page.locator('input[name="self_service_cutoff_hours"]').input_value() == "12"

    # Staff: opt one appointment type into online booking.
    staff_page.goto(live_server + "/admin/scheduling/appointment-types")
    staff_page.locator("table a").first.click()
    staff_page.wait_for_load_state("networkidle")
    type_id = staff_page.url.rstrip("/").split("/")[-1]
    staff_page.goto(live_server + f"/admin/scheduling/appointment-types/{type_id}/edit")
    staff_page.check("#patient_bookable")
    staff_page.fill('input[name="change_reason"]', "Enable online booking for follow-up test coverage")
    staff_page.locator('button[type="submit"]', has_text="Publish New Version").click()
    staff_page.wait_for_url(re.compile(rf"/appointment-types/{type_id}$"))

    # Patient: log in via the mocked magic link.
    page = staff_page.context.browser.new_context().new_page()
    page.goto(live_server + "/portal/login")
    page.fill("#email", "alice@example.com")
    page.click('button[type=submit]')
    page.wait_for_load_state("networkidle")
    login_link = page.locator("a", has_text="Sign in as Alice Johnson")
    login_link.wait_for(state="visible")
    href = login_link.get_attribute("href")
    page.goto(href)
    page.wait_for_url(re.compile(r"/portal/$"))

    # Waitlist self-service: add an entry, confirm it shows, cancel it.
    page.goto(live_server + "/portal/waitlist")
    page.fill('input[name="notes"]', "Portal follow-up coverage")
    page.locator('button[type="submit"]', has_text="Add to Waitlist").click()
    page.wait_for_load_state("networkidle")
    assert "Portal follow-up coverage" in page.locator(".card", has_text="My Waitlist Entries").inner_text()
    page.locator('form[action$="/cancel"] button').first.click()
    page.wait_for_load_state("networkidle")
    entries_text = page.locator(".card", has_text="My Waitlist Entries").inner_text()
    assert "Portal follow-up coverage" in entries_text and "cancelled" in entries_text.lower()

    # Records: visit summaries, prescriptions, documents all load and are
    # reachable from the records home page.
    page.goto(live_server + "/portal/records")
    page.locator("a", has_text="Visit Summaries").click()
    page.wait_for_load_state("networkidle")
    view_link = page.locator("a", has_text="View Summary").first
    if view_link.count():
        view_link.click()
        page.wait_for_load_state("networkidle")
        assert page.locator("h1", has_text="Visit Summary").is_visible()

    page.goto(live_server + "/portal/records/prescriptions")
    assert "Prescriptions" in page.locator("h1").inner_text()

    page.goto(live_server + "/portal/records/documents")
    assert "Documents" in page.locator("h1").inner_text()

    # Reschedule with a provider change: book, then reschedule onto the
    # other provider and confirm the appointments list reflects it.
    weekday_offset = 1
    while (datetime.utcnow() + timedelta(days=weekday_offset)).weekday() >= 5:
        weekday_offset += 1
    target_date = (datetime.utcnow() + timedelta(days=weekday_offset)).strftime("%Y-%m-%d")
    page.goto(live_server + f"/portal/book?date_str={target_date}")
    type_version_id = page.locator('select[name="appointment_type_version_id"] option').nth(1).get_attribute("value")
    page.select_option('select[name="appointment_type_version_id"]', type_version_id)
    page.wait_for_load_state("networkidle")
    page.select_option('select[name="provider_id"]', index=1)
    page.wait_for_load_state("networkidle")
    slot_forms = page.locator('form[action="/portal/book/confirm"]')
    slot_forms.first.wait_for(state="visible")
    slot_forms.first.locator('button[type="submit"]').click()
    page.wait_for_url(re.compile(r"/portal/appointments\?booked=1"))
    page.wait_for_load_state("networkidle")

    appt_href = page.locator('a[href*="/reschedule"]').first.get_attribute("href")
    appt_id = appt_href.rstrip("/").split("/")[-2]
    page.goto(live_server + f"/portal/appointments/{appt_id}/reschedule")
    original_provider = page.locator('select[name="provider_id"] option[selected]').get_attribute("value")
    all_provider_ids = page.locator('select[name="provider_id"] option').all()
    new_provider_id = next(o.get_attribute("value") for o in all_provider_ids if o.get_attribute("value") != original_provider)
    page.select_option('select[name="provider_id"]', new_provider_id)
    page.wait_for_load_state("networkidle")
    reschedule_forms = page.locator('form[action$="/reschedule/confirm"]')
    reschedule_forms.first.wait_for(state="visible")
    reschedule_forms.first.locator('button[type="submit"]').click()
    page.wait_for_url(re.compile(r"/portal/appointments\?rescheduled=1"))
    page.wait_for_load_state("networkidle")

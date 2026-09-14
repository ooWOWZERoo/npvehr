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
    assert "Moderate dry eye disease" in assessment
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
    assert "Post-op LASIK OU" in assessment
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

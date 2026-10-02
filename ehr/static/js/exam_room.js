// New Exam "exam room" (Launchpad redesign) -- behaviour for the layout in
// ehr/templates/exams/form.html. Everything here is presentation: the exam
// form's real fields, the Assessment & Plan composer and the focus-area
// accordion keep living in that template's own inline script, keyed on the
// same ids/names as before. This file only drives the new surface:
// exam-type cards, Habitual/Manifest/Cycloplegic tabs, the focus-area
// summary, and the step rail (scrollspy + "section started" marks).
(function () {
  "use strict";

  var form = document.getElementById("examForm");
  if (!form) return;

  var reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---- Exam-type cards: a radiogroup over the hidden #exam_type_confirmed select ----
  var typeSelect = document.getElementById("exam_type_confirmed");
  var suggestedInput = document.getElementById("suggested_exam_type");
  var cards = Array.prototype.slice.call(document.querySelectorAll("#examTypeCards .type-card"));

  function syncExamType() {
    var current = typeSelect.value;
    var suggested = suggestedInput ? suggestedInput.value : "";
    cards.forEach(function (card, i) {
      var value = card.getAttribute("data-type-value");
      var on = value === current;
      card.setAttribute("aria-checked", on ? "true" : "false");
      // Roving tabindex: the checked card (or the first, when none) is the one tab stop.
      card.tabIndex = on || (!current && i === 0) ? 0 : -1;
      var flag = card.querySelector(".type-card-flag");
      if (flag) flag.hidden = !(suggested && value === suggested);
    });
  }

  function pickExamType(card) {
    typeSelect.value = card.getAttribute("data-type-value");
    // Real events, so the composer treats this as a manual choice and stops
    // overwriting it from later chief-complaint edits (same as the old select).
    typeSelect.dispatchEvent(new Event("input", { bubbles: true }));
    typeSelect.dispatchEvent(new Event("change", { bubbles: true }));
    syncExamType();
  }

  cards.forEach(function (card, i) {
    card.addEventListener("click", function () { pickExamType(card); });
    card.addEventListener("keydown", function (e) {
      var step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
      if (!step) return;
      e.preventDefault();
      var next = cards[(i + step + cards.length) % cards.length];
      next.focus();
      pickExamType(next);
    });
  });
  typeSelect.addEventListener("change", syncExamType);

  // ---- Habitual / Manifest / Cycloplegic tabs (all three groups stay in the DOM and submit) ----
  var tabs = Array.prototype.slice.call(document.querySelectorAll(".rx-tab"));

  function selectTab(tab, focus) {
    var key = tab.getAttribute("data-rx-tab");
    tabs.forEach(function (t) {
      var on = t === tab;
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.tabIndex = on ? 0 : -1;
    });
    document.querySelectorAll("[data-rx-group]").forEach(function (group) {
      group.hidden = group.getAttribute("data-rx-group") !== key;
    });
    if (focus) tab.focus();
  }

  function updateTabDots() {
    tabs.forEach(function (tab) {
      var key = tab.getAttribute("data-rx-tab");
      var has = false;
      document.querySelectorAll('[data-rx-group="' + key + '"] input').forEach(function (input) {
        if ((input.value || "").trim() !== "") has = true;
      });
      tab.classList.toggle("has-values", has);
    });
  }

  tabs.forEach(function (tab, i) {
    tab.addEventListener("click", function () { selectTab(tab, false); });
    tab.addEventListener("keydown", function (e) {
      var target = null;
      if (e.key === "ArrowRight") target = tabs[(i + 1) % tabs.length];
      else if (e.key === "ArrowLeft") target = tabs[(i - 1 + tabs.length) % tabs.length];
      else if (e.key === "Home") target = tabs[0];
      else if (e.key === "End") target = tabs[tabs.length - 1];
      if (!target) return;
      e.preventDefault();
      selectTab(target, true);
    });
  });

  // ---- Focus-area summary + empty state for the assessment panels ----
  var focusToggles = Array.prototype.slice.call(document.querySelectorAll(".focus-toggle"));
  var focusSummary = document.getElementById("focusSummary");
  var areasEmpty = document.getElementById("areasEmpty");

  function syncFocus() {
    var names = focusToggles.filter(function (cb) { return cb.checked; }).map(function (cb) {
      var title = cb.closest(".focus-tile").querySelector(".focus-tile-title");
      return title ? title.textContent : "";
    });
    if (focusSummary) {
      focusSummary.textContent = names.length
        ? names.length + (names.length === 1 ? " area" : " areas") + " selected: " + names.join(" · ")
        : "No focus area selected yet.";
    }
    if (areasEmpty) areasEmpty.hidden = names.length > 0;
  }
  focusToggles.forEach(function (cb) { cb.addEventListener("change", syncFocus); });

  // ---- Patient card in the rail ----
  var patientSelect = form.querySelector('select[name="patient_id"]');
  var railName = document.getElementById("railPatientName");
  var railLink = document.getElementById("railPatientLink");

  function syncPatient() {
    if (!patientSelect || !railName) return;
    var option = patientSelect.options[patientSelect.selectedIndex];
    railName.textContent = option && option.value ? option.text : "—";
    if (railLink) {
      railLink.hidden = !(option && option.value);
      if (option && option.value) railLink.href = "/patients/" + option.value;
    }
  }
  if (patientSelect) patientSelect.addEventListener("change", syncPatient);

  // ---- Step rail: scrollspy + "section started" marks ----
  var sections = Array.prototype.slice.call(document.querySelectorAll("[data-step]"));
  var links = {};
  document.querySelectorAll("[data-step-link]").forEach(function (a) { links[a.getAttribute("data-step-link")] = a; });
  var progressEl = document.getElementById("examProgress");

  // Snapshot every field once, after the template's own first composer pass, so
  // "started" means "changed from how the form opened" -- default selections
  // (e.g. IOP method = Goldmann, the pre-checked Refractive focus) don't count.
  var initial = new Map();
  function fieldKey(f) { return f.type === "checkbox" || f.type === "radio" ? f.checked : f.value; }
  form.querySelectorAll("input, select, textarea").forEach(function (f) { initial.set(f, fieldKey(f)); });

  function sectionStarted(section) {
    var started = false;
    section.querySelectorAll("input, select, textarea").forEach(function (f) {
      if (started || f.type === "hidden" || f.type === "submit" || f.type === "button") return;
      if (f.hasAttribute("data-no-progress")) return;       // helper-only inputs (e.g. ROS suggestions) aren't exam data
      var now = fieldKey(f);
      if (now === initial.get(f)) return;
      if (f.type === "checkbox" || f.type === "radio") started = !!now;
      else started = String(now).trim() !== "";
    });
    return started;
  }

  var progressQueued = false;
  function updateProgress() {
    progressQueued = false;
    var done = 0;
    sections.forEach(function (section) {
      var id = section.getAttribute("data-step");
      var link = links[id];
      if (!link) return;
      var started = sectionStarted(section);
      link.classList.toggle("is-done", started);
      var mark = link.querySelector(".exam-step-mark");
      if (mark) mark.textContent = started ? "✓" : String(sections.indexOf(section) + 1);
      if (started) done++;
    });
    if (progressEl) progressEl.textContent = done + " of " + sections.length + " sections started";
    updateTabDots();
  }
  function queueProgress() {
    if (progressQueued) return;
    progressQueued = true;
    window.requestAnimationFrame(updateProgress);
  }
  // Bubbling listeners on the form run after each field's own listeners, so the
  // composer's auto-filled Assessment/Plan is already in place when this reads it.
  form.addEventListener("input", queueProgress);
  form.addEventListener("change", queueProgress);

  var activeId = null;
  function updateActive() {
    var probe = window.innerHeight * 0.3;
    var current = sections[0];
    sections.forEach(function (section) {
      if (section.getBoundingClientRect().top <= probe) current = section;
    });
    // At the very bottom of the page the last (short) section can never reach
    // the probe line, so its step would never light up -- pick it explicitly.
    var doc = document.documentElement;
    if (window.innerHeight + window.pageYOffset >= doc.scrollHeight - 2) current = sections[sections.length - 1];
    var id = current.getAttribute("data-step");
    if (id === activeId) return;
    activeId = id;
    Object.keys(links).forEach(function (key) {
      var on = key === id;
      links[key].classList.toggle("is-active", on);
      if (on) links[key].setAttribute("aria-current", "step"); else links[key].removeAttribute("aria-current");
    });
  }
  var scrollQueued = false;
  window.addEventListener("scroll", function () {
    if (scrollQueued) return;
    scrollQueued = true;
    window.requestAnimationFrame(function () { scrollQueued = false; updateActive(); });
  }, { passive: true });
  window.addEventListener("resize", updateActive);

  Object.keys(links).forEach(function (key) {
    links[key].addEventListener("click", function (e) {
      var target = document.getElementById("step-" + key);
      if (!target) return;
      e.preventDefault();
      target.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
      if (window.history && window.history.replaceState) window.history.replaceState(null, "", "#step-" + key);
    });
  });

  window.examRoom = { syncExamType: syncExamType };

  syncExamType();
  syncFocus();
  syncPatient();
  updateProgress();
  updateActive();
})();

// New Prescription "rx room" -- the exam-room look for the Rx form
// (ehr/templates/prescriptions/form.html). Presentation only: the form's real
// fields keep their names, and #rx_type stays the single source of truth for
// the Glasses / Contact lenses cards. Rail logic mirrors exam_room.js.
(function () {
  "use strict";

  var form = document.getElementById("rxForm");
  if (!form) return;

  var reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---- Rx-type cards: a radiogroup over the hidden #rx_type select ----
  var typeSelect = document.getElementById("rx_type");
  var cards = Array.prototype.slice.call(document.querySelectorAll("#rxTypeCards .type-card"));
  var contactsSection = document.getElementById("step-contacts");
  var contactsHint = document.getElementById("contactsHint");

  function syncType() {
    var current = typeSelect.value;
    cards.forEach(function (card, i) {
      var on = card.getAttribute("data-type-value") === current;
      card.setAttribute("aria-checked", on ? "true" : "false");
      card.tabIndex = on || (!current && i === 0) ? 0 : -1;
    });
    // Contact parameters stay editable for either type (the server stores them
    // regardless); they are just de-emphasised for a glasses Rx.
    var contacts = current === "contacts";
    if (contactsSection) contactsSection.classList.toggle("is-muted", !contacts);
    if (contactsHint) contactsHint.textContent = contacts ? "" : "Optional for a glasses Rx";
  }

  function pickType(card) {
    typeSelect.value = card.getAttribute("data-type-value");
    typeSelect.dispatchEvent(new Event("input", { bubbles: true }));
    typeSelect.dispatchEvent(new Event("change", { bubbles: true }));
    syncType();
  }

  cards.forEach(function (card, i) {
    card.addEventListener("click", function () { pickType(card); });
    card.addEventListener("keydown", function (e) {
      var step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
      if (!step) return;
      e.preventDefault();
      var next = cards[(i + step + cards.length) % cards.length];
      next.focus();
      pickType(next);
    });
  });
  typeSelect.addEventListener("change", syncType);

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

  syncType();
  syncPatient();
  updateProgress();
  updateActive();
})();

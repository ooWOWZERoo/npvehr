// ROS findings + suggestions on the New Exam page (stages 3 and 4 of the ROS plan).
// Reads /admin/ros/catalog.json and, for the body systems the clinician has answered "Yes" to
// (or any system they open), lists that system's catalog prompts. A ticked prompt is a positive
// finding: it is submitted with the exam as `ros_finding` (the server stores it with the exam)
// and counts as exam progress. Ticking also shows the suggested ICD-10 code(s) and test(s) --
// decision support only: nothing is written into the note and nothing is billed or sent. If the
// catalog can't be loaded the panel simply stays hidden -- this must never get in the way of
// entering an exam.
(function () {
  "use strict";
  var host = document.getElementById("rosAssist");
  var form = document.getElementById("examForm");
  if (!host || !form) return;

  // The exam form's eight Yes/No ROS selects -> the catalog's body systems.
  var SELECT_TO_SYSTEM = {
    constitutional: "Constitutional", cardiovascular: "Cardiovascular", respiratory: "Respiratory",
    gastrointestinal: "Gastrointestinal", neurological: "Neurological", musculoskeletal: "Musculoskeletal",
    endocrine: "Endocrine", skin: "Integumentary"
  };

  var SYSTEM_TO_SELECT = {};
  Object.keys(SELECT_TO_SYSTEM).forEach(function (code) { SYSTEM_TO_SELECT[SELECT_TO_SYSTEM[code]] = code; });

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;        // textContent only: catalog text is data, never markup
    return n;
  }

  function yesSystems() {
    var out = {};
    Object.keys(SELECT_TO_SYSTEM).forEach(function (code) {
      var sel = form.querySelector('select[name="ros_' + code + '"]');
      if (sel && sel.value === "Yes") out[SELECT_TO_SYSTEM[code]] = true;
    });
    return out;
  }

  var catalog = null, promptsById = {}, details = {}, tags = {}, conflicts = {};

  function build(data) {
    var list = host.querySelector("[data-ros-systems]");
    list.replaceChildren();
    data.systems.forEach(function (sys) {
      if (!sys.prompts.length) return;
      var d = el("details", "ros-system");
      var sum = el("summary");
      sum.appendChild(el("span", "ros-system-name", sys.system));
      var tag = el("span", "ros-system-tag", "ROS: Yes"); tag.hidden = true;
      sum.appendChild(tag);
      var conflict = el("span", "ros-conflict", "Summary answer is No, but a finding is ticked"); conflict.hidden = true;
      sum.appendChild(conflict);
      d.appendChild(sum);
      var ul = el("ul", "ros-prompts");
      sys.prompts.forEach(function (p) {
        promptsById[p.id] = p; p.system = sys.system;
        var li = el("li"), label = el("label", "ros-prompt");
        var cb = document.createElement("input");
        cb.type = "checkbox"; cb.value = String(p.id);
        cb.name = "ros_finding";                                   // submitted with the exam (stage 4)
        cb.setAttribute("data-ros-assist", "");
        label.appendChild(cb);
        label.appendChild(document.createTextNode(" " + p.prompt));
        if (p.rules.length) label.appendChild(el("span", "ros-rule-count", p.rules.length + (p.rules.length === 1 ? " suggestion" : " suggestions")));
        li.appendChild(label); ul.appendChild(li);
      });
      d.appendChild(ul);
      list.appendChild(d);
      details[sys.system] = d; tags[sys.system] = tag; conflicts[sys.system] = conflict;
    });
    syncSystems();
    host.hidden = !list.children.length;
  }

  function syncSystems() {
    var yes = yesSystems();
    var wrap = host.querySelector("[data-ros-wrap]");
    if (wrap && Object.keys(yes).length) wrap.open = true;   // a Yes answer opens the panel; never force it closed
    Object.keys(details).forEach(function (system) {
      var on = !!yes[system];
      tags[system].hidden = !on;
      if (on) details[system].open = true;       // open when answered Yes; never force-close what the user opened
    });
  }

  function renderSuggestions() {
    var out = host.querySelector("[data-ros-suggestions]");
    out.replaceChildren();
    var checked = host.querySelectorAll('input[data-ros-assist]:checked');
    if (!checked.length) { out.hidden = true; return; }
    out.hidden = false;
    Array.prototype.forEach.call(checked, function (cb) {
      var p = promptsById[cb.value];
      if (!p) return;
      var card = el("div", "ros-suggest");
      card.appendChild(el("div", "ros-suggest-title", p.prompt));
      if (!p.rules.length) { card.appendChild(el("div", "muted", "No suggestions are set up for this finding.")); }
      p.rules.forEach(function (r) {
        var row = el("div", "ros-suggest-row");
        var codes = el("div", "ros-suggest-codes");
        codes.appendChild(el("span", "mono-data", r.icd10));
        codes.appendChild(document.createTextNode(" → consider "));
        codes.appendChild(el("span", "mono-data", r.cpt));
        if (r.cpt_description) codes.appendChild(document.createTextNode(" " + r.cpt_description));
        row.appendChild(codes);
        if (r.note) row.appendChild(el("div", "muted", r.note));
        var badge = el("span", "badge " + (r.reviewed ? "badge-completed" : "badge-cancelled"), r.reviewed ? "Reviewed" : "Unreviewed — suggestion only");
        if (r.reviewed && r.citation) badge.title = "Source: " + r.citation;
        row.appendChild(badge);
        card.appendChild(row);
      });
      out.appendChild(card);
    });
  }

  // A ticked finding is a deliberate positive. If that system's Yes/No summary is still blank it becomes Yes (blank is
  // not an answer); a "No" is never overwritten -- the mismatch is flagged instead -- and nothing is ever reversed.
  function selectFor(system) {
    var code = SYSTEM_TO_SELECT[system];
    return code ? form.querySelector('select[name="ros_' + code + '"]') : null;
  }

  function reconcileSummary(cb) {
    var p = promptsById[cb.value];
    var sel = p && cb.checked ? selectFor(p.system) : null;
    if (sel && sel.value === "") {
      sel.value = "Yes";
      sel.dispatchEvent(new Event("change", { bubbles: true }));     // so progress, the system tag and any other listener update
    }
  }

  function syncConflicts() {
    Object.keys(conflicts).forEach(function (system) {
      var sel = selectFor(system);
      var ticked = details[system].querySelector('input[data-ros-assist]:checked');
      conflicts[system].hidden = !(sel && sel.value === "No" && ticked);
    });
  }

  host.addEventListener("change", function (e) {
    if (!e.target.matches("input[data-ros-assist]")) return;
    reconcileSummary(e.target);
    renderSuggestions();
    syncConflicts();
  });
  form.addEventListener("change", function (e) { if (e.target.name && e.target.name.indexOf("ros_") === 0) { syncSystems(); syncConflicts(); } });

  fetch("/admin/ros/catalog.json", { credentials: "same-origin", headers: { Accept: "application/json" } })
    .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(function (data) { catalog = data; build(data); })
    .catch(function () { host.hidden = true; });
})();

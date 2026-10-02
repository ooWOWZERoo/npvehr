// Clinical safety warnings on the New Exam page (ROS plan stage 5). For the selected patient, fetches the
// warnings that are eligible to show -- the patient's flag is "yes" and the practice's rule is active AND
// clinician-reviewed -- and shows them in a prominent banner. A rule with a keyword only shows while that
// word appears in the chief complaint, ROS notes, assessment or plan. The banner is informational: it
// never blocks saving. The clinician acknowledges the warnings when signing the visit (the server
// re-evaluates then). If the feed can't load the banner just stays hidden.
(function () {
  "use strict";
  var banner = document.getElementById("safetyBanner");
  var form = document.getElementById("examForm");
  if (!banner || !form) return;
  var sel = form.querySelector('select[name="patient_id"]');
  var fieldNames = ["chief_complaint", "ros_notes", "assessment", "plan"];
  var eligible = [], seq = 0, timer = null;

  function examText() {
    return fieldNames.map(function (n) { var f = form.querySelector('[name="' + n + '"]'); return f ? f.value : ""; }).join(" ").toLowerCase();
  }
  function render() {
    var text = examText();
    var shown = eligible.filter(function (w) { return !w.keyword || text.indexOf(w.keyword.toLowerCase()) !== -1; });
    banner.replaceChildren();
    if (!shown.length) { banner.hidden = true; return; }
    var title = document.createElement("strong");
    title.textContent = "Safety warning" + (shown.length > 1 ? "s" : "") + " for this patient";
    banner.appendChild(title);
    var ul = document.createElement("ul");
    shown.forEach(function (w) {
      var li = document.createElement("li");
      li.textContent = w.warning;                                  // text only: rule wording is data, never markup
      var tag = document.createElement("span");
      tag.className = "muted"; tag.textContent = " (" + w.flag + ")";
      li.appendChild(tag);
      ul.appendChild(li);
    });
    banner.appendChild(ul);
    var note = document.createElement("div");
    note.className = "muted"; note.style.fontSize = "0.8rem";
    note.textContent = "You will be asked to acknowledge these when signing the visit.";
    banner.appendChild(note);
    banner.hidden = false;
  }
  function load() {
    var mine = ++seq, pid = sel && sel.value;
    if (!pid) { eligible = []; render(); return; }
    fetch("/safety/patients/" + encodeURIComponent(pid) + "/warnings.json", { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (d) { if (mine === seq) { eligible = d.warnings || []; render(); } })
      .catch(function () { if (mine === seq) { eligible = []; banner.hidden = true; } });
  }
  if (sel) sel.addEventListener("change", load);
  form.addEventListener("input", function (e) {
    if (e.target && fieldNames.indexOf(e.target.name) !== -1) { clearTimeout(timer); timer = setTimeout(render, 200); }
  });
  load();
})();

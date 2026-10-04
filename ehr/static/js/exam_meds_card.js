// Medications & allergies card on the New Exam page (medication list phase 3). For the selected patient it shows the
// active medications and allergies read-only, says plainly when a list has never been reviewed or has gone stale
// (an empty list means "unknown", not "none"), and lets the clinician confirm a review for this visit: the choice
// is submitted with the exam (med_review_medications / med_review_allergies) and recorded against the saved exam.
// Editing a list happens on the patient's Medications tab (opened in a new tab; the card refreshes when you come
// back). Advisory only: nothing here blocks saving, and if the feed can't load the card simply stays hidden.
(function () {
  "use strict";
  var card = document.getElementById("medsCard");
  var form = document.getElementById("examForm");
  if (!card || !form) return;
  var sel = form.querySelector('select[name="patient_id"]');
  var seq = 0, chosen = {}, lastData = null;
  var OUTCOMES = [["no_changes", "Reviewed — no changes"], ["updated", "Reviewed — updated"], ["none_reported", "Patient reports none"]];

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;          // textContent only: list entries are data, never markup
    return n;
  }
  function hidden(kind) {
    var name = "med_review_" + kind, h = form.querySelector('input[name="' + name + '"]');
    if (!h) { h = document.createElement("input"); h.type = "hidden"; h.name = name; form.appendChild(h); }
    return h;
  }
  function describe(kind, d) {
    var st = d[kind + "_state"], when = d[kind + "_reviewed_at"], label = kind === "medications" ? "Medications" : "Allergies";
    if (st === "never") return el("div", "med-note med-note-info", label + " not yet reviewed. An empty list means unknown, not none.");
    if (st === "stale") return el("div", "med-note med-note-warn", label + " need review (last reviewed " + when + ").");
    return el("div", "muted med-note", label + " reviewed " + when + ".");
  }
  function chips(items, text, extra) {
    var wrap = el("div", "med-chips");
    items.forEach(function (it) { var c = el("span", "med-chip" + (extra ? extra(it) : ""), text(it)); wrap.appendChild(c); });
    return wrap;
  }
  function reviewRow(kind, d) {
    var row = el("div", "med-review");
    var count = (kind === "medications" ? d.medications : d.allergies).length;
    OUTCOMES.forEach(function (o) {
      if (o[0] === "none_reported" && count) return;                 // only offered for an empty list
      var b = el("button", "btn btn-sm " + (chosen[kind] === o[0] ? "btn-primary" : "btn-secondary"), (chosen[kind] === o[0] ? "✓ " : "") + o[1]);
      b.type = "button";
      b.addEventListener("click", function () {
        chosen[kind] = chosen[kind] === o[0] ? "" : o[0];
        hidden(kind).value = chosen[kind];
        render(lastData);
      });
      row.appendChild(b);
    });
    if (chosen[kind]) row.appendChild(el("span", "muted", " Recorded when you save this exam."));
    return row;
  }
  function render(d) {
    lastData = d;
    card.replaceChildren();
    if (!d) { card.hidden = true; return; }
    var head = el("div", "med-head");
    head.appendChild(el("strong", null, "Medications & allergies"));
    var pid = sel && sel.value;
    var link = el("a", "btn btn-sm btn-secondary", "Review / update");
    link.href = "/patients/" + encodeURIComponent(pid) + "/medications"; link.target = "_blank"; link.rel = "noopener";
    head.appendChild(link);
    card.appendChild(head);

    card.appendChild(el("div", "med-label", "Medications"));
    card.appendChild(d.medications.length ? chips(d.medications, function (m) {
      return m.name + (m.strength ? " " + m.strength : "") + (m.eye ? " (" + m.eye + ")" : "") + (m.frequency ? ", " + m.frequency : "");
    }) : el("div", "muted", "None recorded."));
    card.appendChild(describe("medications", d));
    if (d.can_edit) card.appendChild(reviewRow("medications", d));

    card.appendChild(el("div", "med-label", "Allergies"));
    card.appendChild(d.allergies.length ? chips(d.allergies, function (a) {
      return a.allergen + (a.reaction ? " — " + a.reaction : "") + (a.severity ? " (" + a.severity + ")" : "");
    }, function (a) { return a.severity === "severe" ? " med-chip-severe" : ""; }) : el("div", "muted", "None recorded."));
    card.appendChild(describe("allergies", d));
    if (d.can_edit) card.appendChild(reviewRow("allergies", d));
    card.hidden = false;
  }
  function load(keepChoices) {
    var mine = ++seq, pid = sel && sel.value;
    if (!keepChoices) { chosen = {}; hidden("medications").value = ""; hidden("allergies").value = ""; }
    if (!pid) { render(null); return; }
    fetch("/patients/" + encodeURIComponent(pid) + "/medications/summary.json", { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (d) { if (mine === seq) render(d); })
      .catch(function () { if (mine === seq) render(null); });
  }
  if (sel) sel.addEventListener("change", function () { load(false); });
  window.addEventListener("focus", function () { load(true); });     // back from editing the lists in the other tab
  load(false);
})();

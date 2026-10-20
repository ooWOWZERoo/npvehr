// Guided medication intake (Medications tab): medication combobox -> dosage -> frequency, plus the eye modifier.
// Talks only to the JSON API in ehr/routes/medication_intake_api.py (search / dosages / frequencies / add) through
// window.NPV.api; every rule is enforced server-side too, so this file is convenience, not the source of truth.
//   * Medication: debounced 300 ms, searches only once 3+ characters are typed, stale responses are ignored.
//   * Dosage unlocks after a medication is chosen; Frequency after a dosage; Eye as soon as a name exists.
//   * An oral drug sets and locks the eye to PO; PO is not offered for a non-oral catalog drug.
//   * A brand name finds its generic ("Coumadin" -> warfarin); the generic is what gets recorded.
//   * The same form edits an existing medication (the row's Edit button), pre-filled; older free-text rows open as custom entries.
//   * "Add Custom" (always in the list) turns Dosage and Frequency into free-text inputs and records is_custom.
(function () {
  "use strict";
  var root = document.getElementById("medIntake");
  if (!root || !window.NPV || !window.NPV.api) return;

  var MIN_CHARS = 3, DEBOUNCE_MS = 300;
  var pid = root.getAttribute("data-patient-id");
  var $ = function (id) { return document.getElementById(id); };
  var el = {
    name: $("medName"), list: $("medNameList"), nameStatus: $("medNameStatus"),
    eye: $("medEye"), eyeNote: $("medEyeNote"),
    dosage: $("medDosage"), dosageText: $("medDosageText"), dosageNote: $("medDosageNote"),
    freq: $("medFreq"), freqText: $("medFreqText"),
    indication: $("medIndication"), start: $("medStart"), note: $("medNote"),
    submit: $("medSubmit"), back: $("medBackToList"), error: $("medIntakeError"),
    title: $("medIntakeTitle"), help: root.querySelector(".med-intake-help"), cancel: $("medCancelEdit")
  };

  var state = { drug: null, dosage: null, isCustom: false, dosages: [], items: [], active: -1, seq: 0, ctrl: null, editId: null };

  function option(value, text, disabled) {
    var o = document.createElement("option");
    o.value = value; o.textContent = text; if (disabled) o.disabled = true;
    return o;
  }
  function setError(msg) { el.error.textContent = msg || ""; el.error.hidden = !msg; }

  // ---------- results list ----------
  function closeList() {
    el.list.hidden = true; el.list.innerHTML = ""; state.items = []; state.active = -1;
    el.name.setAttribute("aria-expanded", "false"); el.name.removeAttribute("aria-activedescendant");
  }
  function setActive(i) {
    var rows = el.list.children;
    if (!rows.length) return;
    state.active = (i + rows.length) % rows.length;
    for (var k = 0; k < rows.length; k++) rows[k].setAttribute("aria-selected", k === state.active ? "true" : "false");
    el.name.setAttribute("aria-activedescendant", rows[state.active].id);
    rows[state.active].scrollIntoView({ block: "nearest" });
  }
  function renderList(results, query) {
    el.list.innerHTML = ""; state.items = [];
    results.forEach(function (d, i) {
      var li = document.createElement("li");
      li.id = "medOpt" + i; li.setAttribute("role", "option"); li.setAttribute("aria-selected", "false");
      li.className = "med-combo-option";
      var nm = document.createElement("span"); nm.textContent = d.name; li.appendChild(nm);
      if (d.via_brand) { var v = document.createElement("small"); v.className = "muted"; v.textContent = " \u2014 brand: " + d.via_brand; li.appendChild(v); }
      else if (d.route) { var r = document.createElement("small"); r.className = "muted"; r.textContent = " " + d.route; li.appendChild(r); }
      li.addEventListener("mousedown", function (e) { e.preventDefault(); selectDrug(d); });
      el.list.appendChild(li); state.items.push({ drug: d });
    });
    // The escape hatch is always the last row, whatever the search found.
    var c = document.createElement("li");
    c.id = "medOptCustom"; c.setAttribute("role", "option"); c.setAttribute("aria-selected", "false");
    c.className = "med-combo-option med-combo-custom";
    c.textContent = "✨ Add Custom: " + query;
    c.addEventListener("mousedown", function (e) { e.preventDefault(); enterCustom(query); });
    el.list.appendChild(c); state.items.push({ custom: query });
    el.list.hidden = false; el.name.setAttribute("aria-expanded", "true"); state.active = -1;
  }

  // ---------- search ----------
  var search = window.NPV.debounce(function () {
    var q = el.name.value.trim();
    if (q.length < MIN_CHARS) return;
    var seq = ++state.seq;
    if (state.ctrl) state.ctrl.abort();
    state.ctrl = typeof AbortController === "function" ? new AbortController() : null;
    el.nameStatus.textContent = "Searching…";
    window.NPV.api("GET", "/api/v1/drugs/search?q=" + encodeURIComponent(q), null, state.ctrl ? state.ctrl.signal : undefined)
      .then(function (data) {
        if (seq !== state.seq || state.drug || state.isCustom) return;      // a newer keystroke or a choice superseded this
        el.nameStatus.textContent = data.results.length ? "" : "No match for “" + q + "” — use Add Custom below.";
        renderList(data.results, q);
      })
      .catch(function (err) {
        if (err.name === "AbortError" || seq !== state.seq) return;
        el.nameStatus.textContent = "Drug lookup is unavailable right now — use Add Custom.";
        renderList([], q);
      });
  }, DEBOUNCE_MS);

  el.name.addEventListener("input", function () {
    setError("");
    var q = el.name.value;
    if (state.isCustom) { refresh(); return; }                               // custom mode: the text IS the name
    if (state.drug) resetDrug();                                             // editing after a pick starts over
    state.seq++;                                                             // invalidate any in-flight search
    if (state.ctrl) state.ctrl.abort();
    var t = q.trim();
    if (!t) { closeList(); el.nameStatus.textContent = ""; search.cancel(); refresh(); return; }
    if (t.length < MIN_CHARS) {
      search.cancel();
      el.nameStatus.textContent = "Keep typing to search (" + MIN_CHARS + "+ characters), or add it as custom.";
      renderList([], t);
    } else { search(); }
    refresh();
  });
  el.name.addEventListener("keydown", function (e) {
    if (e.key === "ArrowDown" && !el.list.hidden) { e.preventDefault(); setActive(state.active + 1); }
    else if (e.key === "ArrowUp" && !el.list.hidden) { e.preventDefault(); setActive(state.active - 1); }
    else if (e.key === "Enter") {
      e.preventDefault();
      var it = state.items[state.active];
      if (it) { if (it.drug) selectDrug(it.drug); else enterCustom(it.custom); }
    } else if (e.key === "Escape") { closeList(); }
  });
  el.name.addEventListener("blur", function () { setTimeout(closeList, 120); });

  // ---------- chain: drug -> dosage -> frequency, eye ----------
  function resetDrug() {
    state.drug = null; state.dosage = null; state.dosages = [];
    fillSelect(el.dosage, "Choose a medication first"); fillSelect(el.freq, "Choose a dosage first");
    el.dosageNote.textContent = ""; el.eyeNote.textContent = "";
    applyEye(); refresh();
  }
  function fillSelect(sel, placeholder) {
    sel.innerHTML = ""; sel.appendChild(option("", placeholder)); sel.disabled = true;
  }

  // `pre` (edit mode only) = {dosageId, freqId, eye}: once each list has loaded, the saved choice is selected in it.
  function selectDrug(d, pre) {
    state.drug = d; state.dosage = null; state.isCustom = false;
    el.name.value = d.name; closeList(); setError("");
    el.nameStatus.textContent = d.via_brand ? d.via_brand + " is a brand of " + d.name + "." : "";
    fillSelect(el.freq, "Choose a dosage first");
    fillSelect(el.dosage, "Loading…");
    var loaded = window.NPV.api("GET", "/api/v1/drugs/" + d.id + "/dosages").then(function (data) {
      if (state.drug !== d) return;
      state.dosages = data.dosages;
      el.dosage.innerHTML = "";
      el.dosage.appendChild(option("", data.dosages.length ? "Choose a dosage…" : "No dosages on file"));
      data.dosages.forEach(function (x) { el.dosage.appendChild(option(String(x.id), x.label)); });
      el.dosage.disabled = !data.dosages.length;
      el.dosageNote.textContent = "";
      if (!data.dosages.length) {
        var why = data.lookup === "unavailable" ? "RxNorm couldn\u2019t be reached just now \u2014 pick the drug again to retry, or "
                : data.lookup === "not_found" ? "No strengths found for this name \u2014 "
                : "No dosages on file \u2014 ";
        el.dosageNote.appendChild(document.createTextNode(why));
        var b = document.createElement("button");
        b.type = "button"; b.className = "btn btn-sm btn-secondary"; b.textContent = "Enter dosage manually";
        b.addEventListener("click", function () { enterCustom(d.name); });
        el.dosageNote.appendChild(b);
      } else if (pre && pre.dosageId) {
        el.dosage.value = String(pre.dosageId);
        return chooseDosage(pre);
      }
    }).catch(function (err) { if (state.drug === d) { fillSelect(el.dosage, "Could not load dosages"); setError(err.message); } });
    applyEye(); refresh();
    return loaded;
  }

  el.dosage.addEventListener("change", function () { chooseDosage(); });
  function chooseDosage(pre) {
    var id = parseInt(el.dosage.value, 10);
    state.dosage = state.dosages.filter(function (x) { return x.id === id; })[0] || null;
    fillSelect(el.freq, state.dosage ? "Loading…" : "Choose a dosage first");
    applyEye(); refresh();
    if (!state.dosage || !state.drug) return null;
    var d = state.drug, x = state.dosage;
    return window.NPV.api("GET", "/api/v1/drugs/" + d.id + "/frequencies?dosage_id=" + x.id).then(function (data) {
      if (state.drug !== d || state.dosage !== x) return;
      el.freq.innerHTML = ""; el.freq.appendChild(option("", "Choose a frequency…"));
      var pref = data.frequencies.filter(function (f) { return f.preferred; });
      var rest = data.frequencies.filter(function (f) { return !f.preferred; });
      function group(label, rows) {
        if (!rows.length) return;
        var g = label ? document.createElement("optgroup") : el.freq;
        if (label) { g.label = label; el.freq.appendChild(g); }
        rows.forEach(function (f) { g.appendChild(option(String(f.id), f.label)); });
      }
      group(null, pref); group(pref.length ? "Other frequencies" : null, rest);
      el.freq.disabled = false;
      if (pre) {
        if (pre.freqId) el.freq.value = String(pre.freqId);
        if (pre.eye && !el.eye.disabled) el.eye.value = pre.eye;
      }
      refresh();
    }).catch(function (err) { if (state.dosage === x) { fillSelect(el.freq, "Could not load frequencies"); setError(err.message); } });
  }
  el.freq.addEventListener("change", refresh);
  [el.eye, el.dosageText, el.freqText].forEach(function (n) { n.addEventListener("input", refresh); n.addEventListener("change", refresh); });

  // The eye modifier: unlocked once a name exists; an oral drug forces PO; PO is withheld from non-oral catalog drugs.
  function isOral() {
    if (state.isCustom || !state.drug) return false;
    return state.dosage ? !!state.dosage.is_oral : !!state.drug.is_oral;
  }
  function applyEye() {
    var hasName = state.isCustom ? el.name.value.trim().length > 0 : !!state.drug;
    var po = el.eye.querySelector('option[value="PO"]');
    el.eyeNote.textContent = "";
    if (!hasName) { el.eye.value = ""; el.eye.disabled = true; po.disabled = false; return; }
    if (isOral()) {
      el.eye.value = "PO"; el.eye.disabled = true; po.disabled = false;
      el.eyeNote.textContent = "Oral medication — recorded as PO (by mouth).";
      return;
    }
    el.eye.disabled = false;
    po.disabled = !state.isCustom;                                           // custom entries may be any modifier
    if (el.eye.value === "PO" && po.disabled) el.eye.value = "";
  }

  // ---------- custom entry ----------
  function enterCustom(text) {
    state.isCustom = true; state.drug = null; state.dosage = null; state.dosages = [];
    state.seq++; if (state.ctrl) state.ctrl.abort(); search.cancel();
    el.name.value = text; closeList(); el.nameStatus.textContent = "Custom entry — type the dosage and frequency yourself.";
    el.dosage.hidden = true; el.freq.hidden = true;
    el.dosageText.hidden = false; el.freqText.hidden = false; el.dosageNote.textContent = "";
    el.back.hidden = false; setError("");
    applyEye(); refresh(); el.dosageText.focus();
  }
  el.back.addEventListener("click", function () {
    state.isCustom = false;
    el.dosageText.hidden = true; el.freqText.hidden = true; el.dosageText.value = ""; el.freqText.value = "";
    el.dosage.hidden = false; el.freq.hidden = false;
    fillSelect(el.dosage, "Choose a medication first"); fillSelect(el.freq, "Choose a dosage first");
    el.back.hidden = true; el.nameStatus.textContent = ""; el.name.value = ""; el.name.focus();
    applyEye(); refresh();
  });

  // ---------- submit ----------
  function ready() {
    var eye = el.eye.value;
    if (state.isCustom) return el.name.value.trim().length > 0 && !!eye;
    return !!(state.drug && state.dosage && el.freq.value && eye);
  }
  function refresh() { applyEye(); el.submit.disabled = !ready(); }

  el.submit.addEventListener("click", function () {
    if (!ready()) return;
    el.submit.disabled = true; setError("");
    var body = {
      is_custom: state.isCustom, eye: el.eye.value,
      indication: el.indication.value.trim() || null, note: el.note.value.trim() || null, start_date: el.start.value || null
    };
    if (state.isCustom) {
      body.name = el.name.value.trim();
      body.dosage_text = el.dosageText.value.trim() || null;
      body.frequency_text = el.freqText.value.trim() || null;
    } else {
      body.drug_id = state.drug.id; body.dosage_id = state.dosage.id; body.frequency_id = parseInt(el.freq.value, 10);
    }
    var url = "/api/v1/patients/" + encodeURIComponent(pid) + "/medications" + (state.editId ? "/" + state.editId : "");
    window.NPV.api(state.editId ? "PUT" : "POST", url, body)
      .then(function () { window.location.assign("/patients/" + encodeURIComponent(pid) + "/medications"); })
      .catch(function (err) { setError(err.message); el.submit.disabled = !ready(); });
  });

  // ---------- edit an existing medication through the same chain ----------
  var tabUrl = "/patients/" + encodeURIComponent(pid) + "/medications";
  function startEdit(id) {
    setError("");
    window.NPV.api("GET", "/api/v1/patients/" + encodeURIComponent(pid) + "/medications/" + id).then(function (m) {
      state.editId = m.id;
      el.title.textContent = "Edit medication"; el.help.hidden = true;
      el.submit.textContent = "Save changes"; el.cancel.hidden = false;
      el.indication.value = m.indication || ""; el.note.value = m.note || ""; el.start.value = m.start_date || "";
      root.scrollIntoView({ behavior: "smooth", block: "start" });
      if (m.as_custom) {                                            // custom entries, and older free-text rows, are edited as text
        enterCustom(m.name);
        el.dosageText.value = m.strength || ""; el.freqText.value = m.frequency || "";
        if (m.eye) el.eye.value = m.eye;
        refresh();
        return;
      }
      el.name.value = m.drug.name;
      return selectDrug(m.drug, { dosageId: m.dosage_id, freqId: m.frequency_id, eye: m.eye });
    }).catch(function (err) { setError(err.message); });
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest ? e.target.closest("[data-med-edit]") : null;
    if (b) startEdit(b.getAttribute("data-med-edit"));
  });
  el.cancel.addEventListener("click", function () { window.location.assign(tabUrl); });

  applyEye(); refresh();
})();

// New Path Vision EHR -- global layout behavior (vanilla JS, no build step, no deps).
// Covers: sidebar collapse/expand + mobile drawer, live local clock, staff name picker,
// recently-viewed patients (sidebar mini-list), and the top-bar quick patient switcher.
(function () {
  "use strict";

  var shell = document.getElementById("appShell");

  // ---------- Cookie helpers ----------
  // NOTE: `name` here is always one of this file's own hardcoded cookie constants
  // (never user input), so no regex-escaping of `name` is needed before building the pattern.
  function getCookie(name) {
    var parts = document.cookie.split("; ");
    for (var i = 0; i < parts.length; i++) {
      var eq = parts[i].indexOf("=");
      if (eq === -1) continue;
      if (parts[i].substring(0, eq) === name) {
        return decodeURIComponent(parts[i].substring(eq + 1));
      }
    }
    return "";
  }
  function setCookie(name, value, days) {
    var expires = "";
    if (days) {
      var d = new Date();
      d.setTime(d.getTime() + days * 24 * 60 * 60 * 1000);
      expires = "; expires=" + d.toUTCString();
    }
    document.cookie = name + "=" + encodeURIComponent(value) + expires + "; path=/; SameSite=Lax";
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ---------- Sidebar: collapse (desktop, remembered) + drawer (mobile) ----------
  var COLLAPSE_KEY = "npv_sidebar_collapsed";
  var collapseBtn = document.getElementById("sidebarCollapseBtn");
  var hamburgerBtn = document.getElementById("hamburgerBtn");
  var overlay = document.getElementById("sidebarOverlay");

  try {
    if (shell && localStorage.getItem(COLLAPSE_KEY) === "1") {
      shell.classList.add("sidebar-collapsed");
    }
  } catch (e) { /* localStorage unavailable (private mode etc.) -- fall back to expanded */ }

  if (collapseBtn && shell) {
    collapseBtn.addEventListener("click", function () {
      shell.classList.toggle("sidebar-collapsed");
      try {
        localStorage.setItem(COLLAPSE_KEY, shell.classList.contains("sidebar-collapsed") ? "1" : "0");
      } catch (e) {}
    });
  }
  if (hamburgerBtn && shell) {
    hamburgerBtn.addEventListener("click", function () {
      shell.classList.toggle("sidebar-mobile-open");
    });
  }
  if (overlay && shell) {
    overlay.addEventListener("click", function () {
      shell.classList.remove("sidebar-mobile-open");
    });
  }

  // ---------- Sidebar: expandable groups (Appointments / Administration) ----------
  var groupToggles = document.querySelectorAll(".sidebar-expand-toggle");
  for (var gi = 0; gi < groupToggles.length; gi++) {
    (function (btn) {
      btn.addEventListener("click", function () {
        var group = btn.closest(".sidebar-group");
        if (group) group.classList.toggle("open");
      });
    })(groupToggles[gi]);
  }

  // ---------- Live local clock (client-side; deliberately NOT server time) ----------
  var clockEl = document.getElementById("topbarClock");
  function tickClock() {
    if (!clockEl) return;
    var now = new Date();
    clockEl.textContent = now.toLocaleDateString() + " " + now.toLocaleTimeString();
  }
  tickClock();
  setInterval(tickClock, 1000);

  // ---------- Recently-viewed patients (sidebar mini-list, cookie-backed) ----------
  var RECENT_COOKIE = "npv_recent_patients";
  var RECENT_MAX = 5;

  function getRecentPatients() {
    try {
      var raw = getCookie(RECENT_COOKIE);
      var list = raw ? JSON.parse(raw) : [];
      return Array.isArray(list) ? list : [];
    } catch (e) { return []; }
  }
  function recordRecentPatient(id, name) {
    var list = getRecentPatients().filter(function (p) { return String(p.id) !== String(id); });
    list.unshift({ id: id, name: name });
    list = list.slice(0, RECENT_MAX);
    try { setCookie(RECENT_COOKIE, JSON.stringify(list), 365); } catch (e) {}
  }
  window.NPV = window.NPV || {};
  window.NPV.recordRecentPatient = recordRecentPatient;

  function renderRecentPatients() {
    var container = document.getElementById("sidebarRecentPatients");
    if (!container) return;
    var list = getRecentPatients();
    if (!list.length) {
      container.innerHTML = '<div class="sidebar-recent-empty">No recently viewed patients</div>';
      return;
    }
    var html = '<div class="sidebar-recent-heading">Recently viewed</div>';
    for (var i = 0; i < list.length; i++) {
      html += '<a href="/patients/' + encodeURIComponent(list[i].id) + '">' + escapeHtml(list[i].name) + "</a>";
    }
    container.innerHTML = html;
  }
  renderRecentPatients();

  // ---------- Quick patient switcher (top bar type-ahead) ----------
  var switcherInput = document.getElementById("patientSwitcherInput");
  var switcherResults = document.getElementById("patientSwitcherResults");
  var switcherTimer = null;

  if (switcherInput && switcherResults) {
    switcherInput.addEventListener("input", function () {
      var q = switcherInput.value.trim();
      clearTimeout(switcherTimer);
      if (!q) { switcherResults.hidden = true; switcherResults.innerHTML = ""; return; }
      switcherTimer = setTimeout(function () {
        fetch("/patients/search?q=" + encodeURIComponent(q))
          .then(function (r) { return r.json(); })
          .then(function (data) {
            if (!data || !data.length) {
              switcherResults.innerHTML = '<div class="psr-empty">No matches</div>';
            } else {
              var html = "";
              for (var i = 0; i < data.length; i++) {
                var p = data[i];
                html += '<a href="/patients/' + p.id + '">' + escapeHtml(p.name) +
                  (p.dob ? ' <span style="color:#94a3b8">(' + escapeHtml(p.dob) + ")</span>" : "") + "</a>";
              }
              switcherResults.innerHTML = html;
            }
            switcherResults.hidden = false;
          })
          .catch(function () { switcherResults.hidden = true; });
      }, 200);
    });
    document.addEventListener("click", function (e) {
      if (e.target !== switcherInput && !switcherResults.contains(e.target)) {
        switcherResults.hidden = true;
      }
    });
  }
})();

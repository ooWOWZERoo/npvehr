# Vendored library: FullCalendar

`fullcalendar-6.1.19.min.js` is the official **Standard Bundle** (MIT-licensed)
from npm's `fullcalendar@6.1.19` package (`index.global.min.js`), fetched
directly from `registry.npmjs.org` and committed here rather than loaded from
a CDN at runtime -- see BUILD_BACKLOG.md 5a for why (no external runtime
dependency for a clinical app; works in network-restricted environments).

This bundle includes only the free plugins: `@fullcalendar/core`, `list`,
`daygrid`, `timegrid`, `multimonth`, and `interaction` (drag-and-drop/resize).
It does **not** include FullCalendar Premium's `resource-timeline` plugin
(true multi-resource columns), which requires a paid commercial license --
the Calendar & Appointments UX Overhaul's Phase 1 (`appointments/board.html`)
deliberately works around this with a free-tier side-by-side-calendars
approximation instead of buying that license. Revisit only if that
approximation proves too limiting in practice.

**Upgrading**: fetch the new version's tarball from
`https://registry.npmjs.org/fullcalendar/-/fullcalendar-<version>.tgz`,
extract `package/index.global.min.js`, rename to match the new version, update
the `<script src>` in `ehr/templates/appointments/board.html`, and delete the
old file.

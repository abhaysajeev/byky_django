/**
 * Date pickers for every field a drawer spec declares as a date.
 *
 * byky/partials/drawer.html renders kind "date" and kind "datetime" as text
 * inputs carrying .byky-date / .byky-datetime, on the assumption that something
 * upgrades them. This is that something. Without it a date field is a box the
 * user has to type a date into, in whatever format they guess -- which is what
 * every drawer in this project did until 19 Sep 2026, because neither this file
 * nor flatpickr came across in the port.
 *
 * Any screen whose drawer has a date field must load flatpickr and then this
 * file in its vendor_js / page_js blocks. Per-page rather than global, per the
 * rule in layout/partials/scripts.html -- and apps/portal/tests/test_date_
 * fields.py fails the build if a screen declares a date field and forgets them,
 * so the omission cannot go quiet again.
 *
 * Extracted from the wireframe's byky-hrms.js, which initialised the same two
 * classes but was only ever loaded by two HRMS screens.
 */

'use strict';

(function () {
  // No flatpickr on the page is not an error worth breaking a screen over: the
  // field still accepts typing. It is a defect the test catches, not a crash.
  if (typeof flatpickr === 'undefined') return;

  document.querySelectorAll('.byky-date:not(.byky-datetime)').forEach(el =>
    flatpickr(el, { dateFormat: 'd M Y', allowInput: true })
  );

  // Fields that need a time of day alongside the date -- drawer.html's
  // "datetime" kind, e.g. a shift's start and end.
  document.querySelectorAll('.byky-datetime').forEach(el =>
    flatpickr(el, { dateFormat: 'd M Y, H:i', enableTime: true, time_24hr: true, allowInput: true })
  );
})();

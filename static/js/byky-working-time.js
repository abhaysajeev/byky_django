/* byky-working-time.js -- the Branch Working Time screen.
 *
 * Picking a branch loads its week from the server; Save and "Assign to
 * multiple branches" post the week back. Nothing reloads the page.
 *
 * Every server round trip shows the same veil: the table rows blur and a
 * spinner sits over them -- never over the header -- for at least MIN_WAIT,
 * so a fast answer does not flash.
 *
 * The rules checked here mirror apps/company/services.py::validate_schedule.
 * They run first only to point at the bad cells; the server decides.
 *
 * Times use flatpickr in 24-hour mode, never <input type="time">: that shows
 * AM/PM on any computer set to a 12-hour clock, though the screen is 24-hour.
 * Values are always "HH:MM", as the server stores and sends them.
 *
 * Posting, the toast and the error modal come from byky-crud.js
 * (window.BykyCrud), so CSRF handling exists in one place.
 */
(function () {
  'use strict';

  var root = document.querySelector('[data-wt]');
  if (!root) return;

  var MIN_WAIT = 1500;
  var MAX_SHIFTS = 4;
  var DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

  var canUpdate = root.dataset.canUpdate === '1';
  var select = root.querySelector('[data-wt-branch]');
  var grid = root.querySelector('[data-wt-grid]');
  var body = root.querySelector('[data-wt-body]');
  var overlay = root.querySelector('[data-wt-overlay]');
  var overlayText = root.querySelector('[data-wt-overlay-text]');
  var caption = root.querySelector('[data-wt-caption]');
  var cells = Array.prototype.slice.call(root.querySelectorAll('[data-wt-cell]'));
  var saveBtn = root.querySelector('[data-wt-save]');
  var resetBtn = root.querySelector('[data-wt-reset]');
  var assignOpen = root.querySelector('[data-wt-assign-open]');

  var branch = null;          // { id, name } of the loaded branch
  var busy = false;

  /* ── plumbing ─────────────────────────────────────────────────────── */

  function crud() { return window.BykyCrud || {}; }

  function delay(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }

  function getJson(url) {
    return fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (response) {
        return response.json()
          .catch(function () { return { ok: false, errors: [{ field: '', message: 'The server did not answer properly.' }] }; })
          .then(function (json) { return { status: response.status, body: json }; });
      });
  }

  function post(url, payload) {
    if (crud().post) return crud().post(url, payload);
    return Promise.resolve({ status: 0, body: { ok: false, errors: [{ field: '', message: 'Page scripts did not load. Refresh and try again.' }] } });
  }

  function failed() {
    return { status: 0, body: { ok: false, errors: [{ field: '', message: 'Could not reach the server. Check the connection and try again.' }] } };
  }

  function showErrors(title, errors) {
    var items = (errors && errors.length) ? errors : [{ field: '', message: 'Something went wrong.' }];
    if (crud().showMessages) crud().showMessages({ title: title, items: items });
    else window.alert(items.map(function (i) { return (i.field ? i.field + ': ' : '') + i.message; }).join('\n'));
  }

  function toast(message) { if (crud().toast) crud().toast(message); }

  /* The veil covers the rows only: it starts where the header ends. */
  function veil(on, text) {
    if (on) {
      var head = grid.querySelector('thead');
      overlay.style.top = (head ? head.offsetHeight : 0) + 'px';
      overlayText.textContent = text || 'Loading…';
    }
    overlay.hidden = !on;
    body.classList.toggle('is-loading', on);
    busy = on;
    syncButtons();
  }

  /* Holds the veil for at least MIN_WAIT, whatever the network does. */
  function withVeil(text, request) {
    veil(true, text);
    return Promise.all([request.catch(failed), delay(MIN_WAIT)])
      .then(function (results) { return results[0]; })
      .finally(function () { veil(false); });
  }

  function syncButtons() {
    var ready = !!branch && !busy && canUpdate;
    if (saveBtn) saveBtn.disabled = !ready;
    if (resetBtn) resetBtn.disabled = !ready;
    if (assignOpen) assignOpen.disabled = !ready;
    select.disabled = busy;
  }

  /* ── the matrix ───────────────────────────────────────────────────── */

  var TIME = /^([01]\d|2[0-3]):[0-5]\d$/;

  /* "8:30" typed by hand -> "08:30". Anything else is left for validate(). */
  function normalise(value) {
    var v = (value || '').trim();
    return /^\d:\d\d$/.test(v) ? '0' + v : v;
  }

  /* One 24-hour, time-only picker per input. disableMobile stops flatpickr
     handing phones the native time control, which can show AM/PM again. */
  root.querySelectorAll('.scr-shift-time').forEach(function (input) {
    if (typeof flatpickr === 'undefined') return;   // still accepts typed HH:MM
    flatpickr(input, {
      enableTime: true, noCalendar: true, dateFormat: 'H:i', time_24hr: true,
      allowInput: true, minuteIncrement: 1, disableMobile: true
    });
  });

  function setTime(input, value) {
    var fp = input._flatpickr;
    if (fp) {
      if (value) fp.setDate(value, false, 'H:i'); else fp.clear(false);
    }
    input.value = value || '';
  }

  function inputs(cell) {
    return {
      start: cell.querySelector('[data-edge="start"]'),
      end: cell.querySelector('[data-edge="end"]')
    };
  }

  function clearMarks() {
    cells.forEach(function (cell) { cell.classList.remove('is-invalid'); cell.removeAttribute('title'); });
  }

  function fill(shifts) {
    clearMarks();
    var byKey = {};
    (shifts || []).forEach(function (s) { byKey[s.week_day + '-' + s.shift_number] = s; });
    cells.forEach(function (cell) {
      var s = byKey[cell.dataset.day + '-' + cell.dataset.shift];
      var io = inputs(cell);
      setTime(io.start, s ? s.start : '');
      setTime(io.end, s ? s.end : '');
      io.start.disabled = io.end.disabled = !branch || !canUpdate;
    });
  }

  function collect() {
    return cells.map(function (cell) {
      var io = inputs(cell);
      return {
        week_day: Number(cell.dataset.day),
        shift_number: Number(cell.dataset.shift),
        start: normalise(io.start.value),
        end: normalise(io.end.value)
      };
    });
  }

  function cellFor(label) {
    return cells.filter(function (c) { return c.dataset.label === label; })[0];
  }

  /* Same rules as validate_schedule. Times are "HH:MM", so they compare as text. */
  function validate(shifts) {
    var errors = [];
    var byDay = {};
    shifts.forEach(function (s) {
      var label = DAYS[s.week_day] + ' shift ' + s.shift_number;
      if (!s.start && !s.end) return;
      if (!s.start || !s.end) { errors.push({ field: label, message: 'Enter both a start and an end time.' }); return; }
      if (!TIME.test(s.start) || !TIME.test(s.end)) {
        errors.push({ field: label, message: 'Use a 24-hour time such as 08:30.' });
        return;
      }
      if (s.end <= s.start) {
        errors.push({ field: label, message: 'End must be after start. A shift cannot run past midnight; a full day is 00:00 to 23:59.' });
        return;
      }
      (byDay[s.week_day] = byDay[s.week_day] || {})[s.shift_number] = s;
    });
    Object.keys(byDay).forEach(function (day) {
      var shiftsOfDay = byDay[day];
      var previousEnd = null;
      for (var n = 1; n <= MAX_SHIFTS; n++) {
        var s = shiftsOfDay[n];
        if (!s) {
          for (var later = n + 1; later <= MAX_SHIFTS; later++) {
            if (shiftsOfDay[later]) {
              errors.push({ field: DAYS[day] + ' shift ' + later, message: 'Fill shift ' + n + ' first; shifts are added in order.' });
              break;
            }
          }
          break;
        }
        if (previousEnd !== null && s.start <= previousEnd) {
          errors.push({ field: DAYS[day] + ' shift ' + n, message: 'Must start after shift ' + (n - 1) + ' ends (' + previousEnd + ').' });
        }
        previousEnd = s.end;
      }
    });
    return errors;
  }

  function mark(errors) {
    clearMarks();
    errors.forEach(function (e) {
      var cell = cellFor(e.field);
      if (cell) { cell.classList.add('is-invalid'); cell.title = e.message; }
    });
  }

  /* A week that passes the checks, or null after showing what is wrong. */
  function checkedWeek() {
    var week = collect();
    var errors = validate(week);
    mark(errors);
    if (errors.length) { showErrors('Please fix the following', errors); return null; }
    return week;
  }

  // flatpickr reports a picked time as "change"; typing fires "input".
  cells.forEach(function (cell) {
    ['input', 'change'].forEach(function (type) {
      cell.addEventListener(type, function () { cell.classList.remove('is-invalid'); cell.removeAttribute('title'); });
    });
  });

  /* ── load ─────────────────────────────────────────────────────────── */

  function rememberBranch(id) {
    try {
      var url = new URL(window.location.href);
      if (id) url.searchParams.set('branch', id); else url.searchParams.delete('branch');
      window.history.replaceState(null, '', url);
    } catch (err) { /* old browser: the selection just is not kept on refresh */ }
  }

  function setCaption() {
    caption.textContent = branch
      ? branch.name + ' · 24-hour clock; a full day is 00:00 to 23:59. Leave a shift empty if it is not worked.'
      : 'Choose a branch to see its shifts. 24-hour clock; a full day is 00:00 to 23:59.';
  }

  function load(id) {
    rememberBranch(id);
    if (!id) {
      branch = null;
      fill([]);
      setCaption();
      syncButtons();
      return;
    }
    var url = root.dataset.scheduleUrl.replace(/\/0\/$/, '/' + encodeURIComponent(id) + '/');
    withVeil('Loading schedule…', getJson(url)).then(function (res) {
      if (res.body && res.body.ok) {
        branch = res.body.branch;
        fill(res.body.shifts);
      } else {
        branch = null;
        fill([]);
        showErrors('Could not load the schedule', res.body && res.body.errors);
      }
      setCaption();
      syncButtons();
    });
  }

  select.addEventListener('change', function () { load(select.value); });
  if (resetBtn) resetBtn.addEventListener('click', function () { if (branch) load(String(branch.id)); });

  /* ── save ─────────────────────────────────────────────────────────── */

  function markScheduleSet(ids) {
    ids.forEach(function (id) {
      var row = root.querySelector('[data-wt-assign-row][data-id="' + id + '"]');
      var cell = row && row.lastElementChild;
      if (cell) cell.innerHTML = '<span class="scr-badge scr-badge-active"><i></i>Set</span>';
    });
  }

  if (saveBtn) saveBtn.addEventListener('click', function () {
    if (!branch || busy) return;
    var week = checkedWeek();
    if (!week) return;
    withVeil('Saving schedule…', post(root.dataset.saveUrl, { branch: branch.id, shifts: week }))
      .then(function (res) {
        if (res.body && res.body.ok) {
          fill(res.body.shifts);
          markScheduleSet([branch.id]);
          toast(res.body.message || 'Schedule saved.');
        } else {
          mark((res.body && res.body.errors) || []);
          showErrors('Could not save', res.body && res.body.errors);
        }
      });
  });

  /* ── start ── before the assign section, which returns early without edit rights */

  setCaption();
  syncButtons();
  if (select.value) load(select.value);

  /* ── assign to multiple branches ──────────────────────────────────── */

  var modal = root.querySelector('[data-scr-modal="wt-assign"]');
  if (!modal || !assignOpen) return;

  var modalVeil = root.querySelector('[data-scr-modal-veil="wt-assign"]');
  var search = modal.querySelector('[data-wt-assign-search]');
  var all = modal.querySelector('[data-wt-assign-all]');
  var allLabel = modal.querySelector('[data-wt-assign-all-label]');
  var count = modal.querySelector('[data-wt-assign-count]');
  var empty = modal.querySelector('[data-wt-assign-empty]');
  var go = modal.querySelector('[data-wt-assign-go]');
  var sub = modal.querySelector('[data-wt-assign-sub]');
  var rows = Array.prototype.slice.call(modal.querySelectorAll('[data-wt-assign-row]'));

  function box(row) { return row.querySelector('input[type="checkbox"]'); }
  function available(row) { return !row.dataset.self; }
  function visible(row) { return available(row) && !row.hidden; }

  function syncAssign() {
    var shown = rows.filter(visible);
    var picked = rows.filter(function (r) { return available(r) && box(r).checked; });
    var shownPicked = shown.filter(function (r) { return box(r).checked; });

    all.checked = shown.length > 0 && shownPicked.length === shown.length;
    all.indeterminate = shownPicked.length > 0 && shownPicked.length < shown.length;
    all.disabled = shown.length === 0;
    allLabel.textContent = search.value.trim()
      ? 'All matching (' + shown.length + ')'
      : 'All branches (' + shown.length + ')';
    count.textContent = picked.length + ' selected';
    empty.hidden = shown.length > 0;
    go.disabled = picked.length === 0;
    go.textContent = picked.length
      ? 'Assign to ' + picked.length + ' branch' + (picked.length === 1 ? '' : 'es')
      : 'Assign';
  }

  function applySearch() {
    var terms = search.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    rows.forEach(function (row) {
      if (!available(row)) { row.hidden = true; return; }
      var text = row.dataset.search;
      row.hidden = !terms.every(function (t) { return text.indexOf(t) !== -1; });
    });
    syncAssign();
  }

  function openAssign() {
    rows.forEach(function (row) {
      var self = String(row.dataset.id) === String(branch.id);
      if (self) row.dataset.self = '1'; else delete row.dataset.self;
      box(row).checked = false;
    });
    search.value = '';
    sub.textContent = 'Copies ' + branch.name + '’s weekly shifts, as shown on screen, to the branches you pick.';
    applySearch();
    modal.hidden = false;
    if (modalVeil) modalVeil.hidden = false;
    setTimeout(function () { search.focus(); }, 50);
  }

  function closeAssign() {
    modal.hidden = true;
    if (modalVeil) modalVeil.hidden = true;
  }

  assignOpen.addEventListener('click', function () {
    if (!branch || busy) return;
    if (!checkedWeek()) return;       // assign what is on screen, so it must be valid
    openAssign();
  });

  search.addEventListener('input', applySearch);
  // Escape clears the search first; only an empty search lets it close the
  // modal (byky-screen.js closes any open modal on Escape).
  search.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && search.value) {
      e.preventDefault();
      e.stopPropagation();
      search.value = '';
      applySearch();
    }
  });
  // Enter must not submit or close anything.
  search.addEventListener('keydown', function (e) { if (e.key === 'Enter') e.preventDefault(); });

  all.addEventListener('change', function () {
    rows.filter(visible).forEach(function (row) { box(row).checked = all.checked; });
    syncAssign();
  });

  rows.forEach(function (row) {
    box(row).addEventListener('change', syncAssign);
    // The whole row toggles, not just the 17px box.
    row.addEventListener('click', function (e) {
      if (e.target.tagName === 'INPUT') return;
      box(row).checked = !box(row).checked;
      syncAssign();
    });
  });

  go.addEventListener('click', function () {
    var week = checkedWeek();
    if (!week) return;
    var ids = rows.filter(function (r) { return available(r) && box(r).checked; })
      .map(function (r) { return Number(r.dataset.id); });
    if (!ids.length) return;
    closeAssign();
    withVeil('Assigning schedule…', post(root.dataset.assignUrl, { branches: ids, shifts: week }))
      .then(function (res) {
        if (res.body && res.body.ok) {
          markScheduleSet(res.body.branches || ids);
          toast(res.body.message || 'Schedule assigned.');
        } else {
          showErrors('Could not assign', res.body && res.body.errors);
        }
      });
  });
})();

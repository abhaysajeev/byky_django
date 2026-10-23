/**
 * Duty Roster -- design/duty roster/duty-roster.md.
 *
 * Ported from the client's own wireframe
 * (byky-main/.../src/assets/js/byky-hrms-duty-roster.js), rebuilt against
 * real data and real writes:
 *
 *   - R.matrix[emp_no][iso_date] exists only for a day that has an actual
 *     DutyRoster row -- there is no deterministic hash filling every day, so
 *     an unset day stays visibly blank everywhere (State table, Branch Roster
 *     chips, the Employee View calendar).
 *   - Employees carry no fixed branch (duty-roster.md: "employees are not
 *     branch-mapped"), unlike the wireframe's `branchEmployees[branch]`.
 *     Which branch someone is working is read per day, from the matrix --
 *     see workingOnDay() below.
 *   - Every mutation (Edit Day, Add to Day, Add Employee Roster's week,
 *     Bulk Import's Apply) is a real POST to apps/crew/writes.py, not an
 *     in-memory edit of R. Every one but Bulk Import reloads the page on
 *     success, the same convention byky-crud.js uses everywhere else on this
 *     site -- the State/Branch/Employee tabs, the KPI tiles and R itself all
 *     derive from one server-rendered payload, and re-deriving pieces of it
 *     in JS after a save is how they drift apart. Bulk Import is the one
 *     exception: it shows each row's real backend result in place, so a
 *     "Reload" prompt replaces its Apply button afterwards instead of an
 *     automatic reload.
 */
'use strict';

(function () {
  var dataEl = document.getElementById('roster-json-data');
  var root = document.querySelector('[data-duty-roster]');
  if (!dataEl || !root) return;
  var R = JSON.parse(dataEl.textContent);

  var URLS = {
    branchShifts: root.dataset.urlBranchShifts,
    saveWeek: root.dataset.urlSaveWeek,
    assignDay: root.dataset.urlAssignDay,
    removeDay: root.dataset.urlRemoveDay,
    import: root.dataset.urlImport,
  };

  var Crud = window.BykyCrud || {};

  // DutyRosterDayType's fixed vocabulary (apps/crew/models.py) -- the matrix
  // carries the *label* (get_day_type_display()), every write endpoint wants
  // the *raw* value. Small and fixed enough to keep as one lookup here
  // rather than threading a json_script blob through every drawer for it.
  var DAY_TYPE_VALUE = {
    'Working': 'working', 'Week Off': 'week_off',
    'Sick Leave': 'sick_leave', 'Casual Leave': 'casual_leave',
  };
  var DAY_TYPE_OPTIONS = [
    ['', 'Not set'], ['working', 'Working'], ['week_off', 'Week Off'],
    ['sick_leave', 'Sick Leave'], ['casual_leave', 'Casual Leave'],
  ];
  // Set only by an approved leave request (apps/crew/leave.py): shown, never
  // offered in a dropdown, and locked -- the server refuses any change.
  var ON_LEAVE = 'On Leave';
  function isLeave(rec) { return !!(rec && rec.status === ON_LEAVE); }
  function statusText(rec) {
    return isLeave(rec) && rec.leave_type ? ON_LEAVE + ' · ' + rec.leave_type : rec.status;
  }

  var empByNo = {}, empById = {}, empByName = {};
  R.employees.forEach(function (e) {
    empByNo[e.emp_no] = e; empById[e.id] = e; empByName[e.name] = e;
  });

  var branchNameToId = {}, branchIdToName = {};
  var branchesEl = document.getElementById('roster-branches-data');
  var ALL_BRANCHES = branchesEl ? JSON.parse(branchesEl.textContent) : [];
  ALL_BRANCHES.forEach(function (b) { branchNameToId[b.name] = b.id; branchIdToName[b.id] = b.name; });

  var currentWeek = R.weeks[R.current_week_index] || R.weeks[0];

  function dayLabel(iso) {
    var d = new Date(iso + 'T00:00:00');
    return d.toLocaleDateString('en-US', { weekday: 'short' }) + ' ' + d.getDate();
  }
  function fullDayLabel(iso) {
    var d = new Date(iso + 'T00:00:00');
    return d.toLocaleDateString('en-US', { weekday: 'long', day: 'numeric', month: 'short', year: 'numeric' });
  }
  function statusBadgeClass(status) {
    if (status === 'Working') return 'scr-badge-approved';
    if (status === 'Sick Leave' || status === 'Casual Leave' || status === ON_LEAVE) return 'scr-badge-rejected';
    return 'scr-badge-pending';
  }
  function shiftLabel(rec) {
    if (!rec || rec.status !== 'Working' || !rec.shift1_start) return '';
    return rec.shift1_start + ' – ' + rec.shift1_end;
  }

  // Which branch someone is working is a fact about one day, never about the
  // employee -- read straight from the matrix, the structural adaptation
  // this whole file makes from the wireframe's static branchEmployees[branch].
  function workingOnDay(branchName, dateIso) {
    var out = [];
    R.employees.forEach(function (e) {
      var rec = (R.matrix[e.emp_no] || {})[dateIso];
      if (rec && rec.status === 'Working' && rec.branch === branchName) out.push({ emp: e, rec: rec });
    });
    return out;
  }

  function branchShiftDefaults(branchId, dateIso) {
    if (!branchId || !dateIso || !URLS.branchShifts) return Promise.resolve({ shift1: null, shift2: null });
    var url = URLS.branchShifts + '?branch=' + encodeURIComponent(branchId) + '&date=' + encodeURIComponent(dateIso);
    return fetch(url, { credentials: 'same-origin' }).then(function (r) { return r.json(); }).catch(function () { return { shift1: null, shift2: null }; });
  }

  // ---- the day popup -----------------------------------------------------
  // The house modal (theme/templates/byky/partials/detail_modal.html): a
  // title and a subline, an optional status pill, label/value rows, an
  // optional note, and an optional primary action.
  var detailModal = document.getElementById('scr-detail-modal');
  var detailVeil = document.getElementById('scr-detail-veil');
  var detailAction = null;
  function closeDetailModal() {
    if (!detailModal) return;
    detailModal.hidden = true;
    if (detailVeil) detailVeil.hidden = true;
    detailAction = null;
  }
  function openDetailModal(opts) {
    if (!detailModal) return;
    document.getElementById('scr-detail-modal-title').textContent = opts.title;
    document.getElementById('scr-detail-modal-sub').textContent = opts.sub || '';
    document.getElementById('scr-detail-badge').innerHTML = opts.badge || '';
    var rows = document.getElementById('scr-detail-rows');
    rows.innerHTML = '';
    var shown = 0;
    (opts.fields || []).forEach(function (pair) {
      if (pair[1] === '' || pair[1] == null) return;
      shown++;
      var row = document.createElement('div');
      row.className = 'scr-detail-row';
      row.innerHTML = '<span class="scr-detail-label">' + pair[0] + '</span><span class="scr-detail-value">' + pair[1] + '</span>';
      rows.appendChild(row);
    });
    var empty = document.getElementById('scr-detail-empty');
    empty.textContent = opts.empty || 'No details.';
    empty.hidden = shown > 0;
    var note = document.getElementById('scr-detail-note');
    note.textContent = opts.note || '';
    note.hidden = !opts.note;
    var action = document.getElementById('scr-detail-action');
    detailAction = opts.action || null;
    action.hidden = !detailAction;
    action.textContent = detailAction ? detailAction.label : '';
    detailModal.hidden = false;
    if (detailVeil) detailVeil.hidden = false;
    (detailAction ? action : detailModal.querySelector('[data-detail-close]')).focus();
  }
  if (detailModal) {
    document.addEventListener('click', function (e) {
      if (e.target.closest('[data-detail-close]') || e.target === detailVeil) closeDetailModal();
      if (e.target.closest('#scr-detail-action') && detailAction) {
        var run = detailAction.run;
        closeDetailModal();
        run();
      }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !detailModal.hidden) closeDetailModal();
    });
  }

  // ============================================================
  // Edit Day drawer -- opened from the Employee View calendar's pencil icon
  // ============================================================
  var editDayDrawer = document.getElementById('drawerDutyRosterDay');
  var editDayContext = null;

  function openEditDay(emp, dateIso) {
    if (!editDayDrawer || !editDayDrawer.__bykyFill) return;
    var rec = (R.matrix[emp.emp_no] || {})[dateIso];
    editDayContext = { employeeId: emp.id, dateIso: dateIso };
    var record = {
      employee_label: emp.name + ' — ' + dayLabel(dateIso),
      day_type: rec ? (DAY_TYPE_VALUE[rec.status] || 'working') : 'working',
      branch: rec && rec.branch ? (branchNameToId[rec.branch] || '') : '',
      shift1_start_hhmm: (rec && rec.shift1_start) || '',
      shift1_end_hhmm: (rec && rec.shift1_end) || '',
      add_shift2: !!(rec && rec.shift2_start),
      shift2_start_hhmm: (rec && rec.shift2_start) || '',
      shift2_end_hhmm: (rec && rec.shift2_end) || '',
    };
    editDayDrawer.__bykyFill('edit', record);
    var host = editDayDrawer.querySelector('[data-days-host="employee_key"]');
    if (host) host.innerHTML = '<div class="scr-help" style="padding:0">' + emp.name + ' (' + emp.emp_no + ') · ' + fullDayLabel(dateIso) + '</div>';
    if (window.bootstrap && bootstrap.Offcanvas) bootstrap.Offcanvas.getOrCreateInstance(editDayDrawer).show();
  }

  if (editDayDrawer) {
    editDayDrawer.addEventListener('click', function (e) {
      var button = e.target.closest('[data-scr-save]');
      if (!button || !editDayContext) return;
      e.preventDefault();

      var val = function (name) { var el = editDayDrawer.querySelector('[data-field="' + name + '"]'); return el ? el.value : ''; };
      var checked = function (name) { var el = editDayDrawer.querySelector('[data-field="' + name + '"]'); return !!(el && el.checked); };
      var dayType = val('day_type');
      var working = dayType === 'working';
      var hasShift2 = working && checked('add_shift2');

      button.disabled = true;
      Crud.post(URLS.assignDay, {
        employee: editDayContext.employeeId,
        date: editDayContext.dateIso,
        day_type: dayType,
        branch: working ? (val('branch') || null) : null,
        shift1_start: working ? (val('shift1_start_hhmm') || null) : null,
        shift1_end: working ? (val('shift1_end_hhmm') || null) : null,
        shift2_start: hasShift2 ? (val('shift2_start_hhmm') || null) : null,
        shift2_end: hasShift2 ? (val('shift2_end_hhmm') || null) : null,
      }).then(function (result) {
        button.disabled = false;
        if (result.body.ok) { Crud.toastAfterReload(result.body.message); window.location.reload(); return; }
        Crud.showMessages({
          title: result.status === 403 ? 'Not allowed' : 'Please fix the following',
          items: result.body.errors || [{ field: '', message: 'Something went wrong.' }],
        });
      });
    });
  }

  // ============================================================
  // Branch Roster tab
  // ============================================================
  function shortDate(iso) {
    var d = new Date(iso + 'T00:00:00');
    return d.getDate() + ' ' + d.toLocaleDateString('en-US', { month: 'short' });
  }
  function weekLabel(week) {
    var a = new Date(week.days[0] + 'T00:00:00'), b = new Date(week.days[week.days.length - 1] + 'T00:00:00');
    return a.getMonth() === b.getMonth()
      ? a.getDate() + '–' + b.getDate() + ' ' + b.toLocaleDateString('en-US', { month: 'short' })
      : shortDate(week.days[0]) + ' – ' + shortDate(week.days[week.days.length - 1]);
  }
  function shiftLines(rec) {
    if (!rec || rec.status !== 'Working' || !rec.shift1_start) return '';
    return '<span class="scr-wk-chip-time">' + rec.shift1_start + '–' + rec.shift1_end + '</span>' +
      (rec.shift2_start ? '<span class="scr-wk-chip-time">' + rec.shift2_start + '–' + rec.shift2_end + '</span>' : '');
  }

  // One day of one station: the cover in words first (the gap a planner is
  // looking for is a day with staff and no cashier), then the day's Cashiers
  // and its Labour, each under its own label with its own Add.
  function renderWeekGrid(host, branchName, week) {
    if (!host) return;
    host.innerHTML = '';
    week.days.forEach(function (day) {
      var working = workingOnDay(branchName, day);
      var cashiers = working.filter(function (w) { return w.emp.role === 'Cashier'; });
      var labour = working.filter(function (w) { return w.emp.role !== 'Cashier'; });
      var d = new Date(day + 'T00:00:00');

      var card = document.createElement('div');
      card.className = 'scr-wk-day-card' + (day === R.today ? ' is-today' : '');
      card.dataset.day = day;

      var cover;
      if (!working.length) cover = '<div class="scr-wk-cover is-none">No one rostered</div>';
      else if (!cashiers.length) cover = '<div class="scr-wk-cover is-gap">No cashier</div>';
      else cover = '<div class="scr-wk-cover">' + cashiers.length + ' cashier' + (cashiers.length > 1 ? 's' : '') +
        ' · ' + labour.length + ' labour</div>';

      function chip(w, role) {
        return '<div class="scr-wk-chip is-' + role + '" data-emp="' + w.emp.id + '" title="' + w.emp.name + ' (' + w.emp.emp_no + ')">' +
          '<div class="scr-wk-chip-main"><span class="scr-wk-chip-name">' + w.emp.name + '</span>' + shiftLines(w.rec) + '</div>' +
          '<button type="button" class="scr-wk-chip-x" title="Remove from ' + shortDate(day) + '" aria-label="Remove ' + w.emp.name + ' from ' + shortDate(day) + '" data-wk-remove="' + w.emp.id + '">' +
          '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"></path></svg></button></div>';
      }
      function role(name, people) {
        var key = name === 'Cashier' ? 'cashier' : 'labour';
        return '<div class="scr-wk-role">' +
          '<div class="scr-wk-role-head"><i class="scr-roster-dot is-' + key + '"></i>' + (name === 'Cashier' ? 'Cashiers' : 'Labour') + '</div>' +
          people.map(function (w) { return chip(w, key); }).join('') +
          '<button type="button" class="scr-wk-add" data-wk-add="' + name + '" data-wk-day="' + day + '">+ Add ' + name.toLowerCase() + '</button></div>';
      }

      card.innerHTML = '<div class="scr-wk-day-head"><span>' + d.toLocaleDateString('en-US', { weekday: 'short' }) + '</span>' +
        '<span class="scr-wk-day-date">' + shortDate(day) + (day === R.today ? ' · today' : '') + '</span></div>' +
        cover + role('Cashier', cashiers) + role('Labour', labour);
      host.appendChild(card);
    });
  }

  var branchGrid = document.getElementById('roster-branch-grid');
  var branchSelect = document.getElementById('roster-branch-select');
  var addToDayDrawer = document.getElementById('drawerDutyRosterAddToDay');
  var addToDayContext = null;

  var weekPick = document.getElementById('roster-week-select');
  var query = new URLSearchParams(window.location.search);

  function remember() {
    query.set('station', branchSelect.value);
    query.set('week', String(R.weeks.indexOf(currentWeek) + 1));
    window.history.replaceState(null, '', '?' + query.toString());
  }

  if (weekPick && R.weeks.length) {
    var first = new Date(R.weeks[0].days[0] + 'T00:00:00');
    var shown = { year: first.getFullYear(), month: first.getMonth() + 1 };
    var weekFromAddress = R.weeks[(parseInt(query.get('week'), 10) || 0) - 1];
    if (weekFromAddress) currentWeek = weekFromAddress;
    weekPick.innerHTML = '<option value="prev">‹ Previous month</option>' +
      R.weeks.map(function (w, i) {
        var thisWeek = w.days.indexOf(R.today) >= 0;
        return '<option value="' + i + '"' + (w === currentWeek ? ' selected' : '') + '>' +
          weekLabel(w) + (thisWeek ? ' · this week' : '') + '</option>';
      }).join('') +
      '<option value="next">Next month ›</option>';
    weekPick.addEventListener('change', function () {
      if (weekPick.value === 'prev' || weekPick.value === 'next') {
        var step = weekPick.value === 'next' ? 1 : -1;
        var month = shown.month + step, year = shown.year;
        if (month === 0) { month = 12; year -= 1; }
        if (month === 13) { month = 1; year += 1; }
        var next = new URLSearchParams({ year: String(year), month: String(month), station: branchSelect.value, week: '1' });
        window.location.search = '?' + next.toString();
        return;
      }
      currentWeek = R.weeks[+weekPick.value];
      remember();
      renderWeekGrid(branchGrid, branchSelect.value, currentWeek);
    });
  }

  if (branchGrid && branchSelect) {
    if (query.get('station') && Array.prototype.some.call(branchSelect.options, function (o) { return o.value === query.get('station'); })) {
      branchSelect.value = query.get('station');
    }
    branchSelect.addEventListener('change', function () {
      remember();
      renderWeekGrid(branchGrid, branchSelect.value, currentWeek);
    });
    renderWeekGrid(branchGrid, branchSelect.value, currentWeek);

    branchGrid.addEventListener('click', function (e) {
      var addBtn = e.target.closest('[data-wk-add]');
      if (addBtn && addToDayDrawer && addToDayDrawer.__bykyFill) {
        var branchName = branchSelect.value;
        var dateIso = addBtn.dataset.wkDay;
        addToDayContext = { branchId: branchNameToId[branchName] || '', branchName: branchName, dateIso: dateIso };
        addToDayDrawer.__bykyFill('add', null);
        // Pre-set to whichever button was clicked -- Role also narrows the
        // Employee combo below it (drawer.html's combo_filter_field), so
        // clicking +Cashier only ever offers Cashiers to pick from, editable
        // if that was the wrong button.
        var categorySelect = addToDayDrawer.querySelector('[data-field="category"]');
        if (categorySelect) {
          categorySelect.value = (addBtn.dataset.wkAdd || '').toLowerCase();
          categorySelect.classList.toggle('has-value', !!categorySelect.value);
          categorySelect.dispatchEvent(new Event('change', { bubbles: true }));
        }
        var branchHost = addToDayDrawer.querySelector('[data-days-host="branch"]');
        var dateHost = addToDayDrawer.querySelector('[data-days-host="for_date"]');
        if (branchHost) branchHost.innerHTML = '<div class="scr-help" style="padding:0"><b>' + branchName + '</b></div>';
        if (dateHost) dateHost.innerHTML = '<div class="scr-help" style="padding:0">' + fullDayLabel(dateIso) + '</div>';
        if (window.bootstrap && bootstrap.Offcanvas) bootstrap.Offcanvas.getOrCreateInstance(addToDayDrawer).show();
        return;
      }
      var removeBtn = e.target.closest('[data-wk-remove]');
      if (removeBtn) {
        var emp = empById[removeBtn.dataset.wkRemove];
        var day = removeBtn.closest('.scr-wk-day-card').dataset.day;
        if (!emp) return;
        removeBtn.disabled = true;
        Crud.post(URLS.removeDay, { employee: emp.id, date: day }).then(function (result) {
          if (result.body.ok) { Crud.toastAfterReload(result.body.message); window.location.reload(); return; }
          removeBtn.disabled = false;
          Crud.showMessages({
            title: result.status === 403 ? 'Not allowed' : 'Please fix the following',
            items: result.body.errors || [{ field: '', message: 'Something went wrong.' }],
          });
        });
      }
    });
  }

  if (addToDayDrawer) {
    var addEmployeeHidden = addToDayDrawer.querySelector('[data-field="employee_key"]');
    var addShift1Start = addToDayDrawer.querySelector('[data-field="shift1_start_hhmm"]');
    var addShift1End = addToDayDrawer.querySelector('[data-field="shift1_end_hhmm"]');
    var addShift2Start = addToDayDrawer.querySelector('[data-field="shift2_start_hhmm"]');
    var addShift2End = addToDayDrawer.querySelector('[data-field="shift2_end_hhmm"]');

    if (addEmployeeHidden) {
      addEmployeeHidden.addEventListener('change', function () {
        if (!addToDayContext || !addToDayContext.branchId) return;
        if (addShift1Start && addShift1Start.value) return;
        branchShiftDefaults(addToDayContext.branchId, addToDayContext.dateIso).then(function (defaults) {
          if (defaults.shift1 && addShift1Start && !addShift1Start.value) { addShift1Start.value = defaults.shift1.start; addShift1End.value = defaults.shift1.end; }
        });
      });
    }

    addToDayDrawer.addEventListener('click', function (e) {
      var button = e.target.closest('[data-scr-save]');
      if (!button || !addToDayContext) return;
      e.preventDefault();

      var empName = addEmployeeHidden ? addEmployeeHidden.value : '';
      var emp = empByName[empName];
      if (!emp) {
        Crud.showMessages({ title: 'Please fix the following', items: [{ field: 'Employee', message: 'Search and pick who this day is for.' }] });
        return;
      }
      var checked = function (name) { var el = addToDayDrawer.querySelector('[data-field="' + name + '"]'); return !!(el && el.checked); };
      var hasShift2 = checked('add_shift2');
      var again = button.dataset.scrSave === 'new';

      button.disabled = true;
      Crud.post(URLS.assignDay, {
        employee: emp.id,
        date: addToDayContext.dateIso,
        day_type: 'working',
        branch: addToDayContext.branchId || null,
        shift1_start: addShift1Start ? addShift1Start.value || null : null,
        shift1_end: addShift1End ? addShift1End.value || null : null,
        shift2_start: hasShift2 && addShift2Start ? addShift2Start.value || null : null,
        shift2_end: hasShift2 && addShift2End ? addShift2End.value || null : null,
        // Add to Day only ever introduces someone new to a day -- Edit Day
        // is the one place a day is meant to change. Without this,
        // re-picking someone already on the roster that day silently
        // overwrote whatever was there (a different branch or shift) with
        // no warning at all.
        require_new: true,
      }).then(function (result) {
        button.disabled = false;
        if (result.body.ok) {
          if (again) {
            // Employee and shift only -- Role/branch/date stay put, so
            // adding several Cashiers to the same day in a row doesn't
            // mean re-picking the branch and role every time.
            if (addEmployeeHidden) { addEmployeeHidden.value = ''; addEmployeeHidden.dispatchEvent(new Event('change', { bubbles: true })); }
            var addInput = addToDayDrawer.querySelector('.scr-combo-input');
            if (addInput) addInput.value = '';
            if (addShift1Start) addShift1Start.value = '';
            if (addShift1End) addShift1End.value = '';
            if (addShift2Start) addShift2Start.value = '';
            if (addShift2End) addShift2End.value = '';
            var shift2Toggle = addToDayDrawer.querySelector('[data-field="add_shift2"]');
            if (shift2Toggle) { shift2Toggle.checked = false; shift2Toggle.dispatchEvent(new Event('change')); }
            Crud.toast(result.body.message);
            return;
          }
          Crud.toastAfterReload(result.body.message);
          window.location.reload();
          return;
        }
        Crud.showMessages({
          title: result.status === 403 ? 'Not allowed' : 'Please fix the following',
          items: result.body.errors || [{ field: '', message: 'Something went wrong.' }],
        });
      });
    });
  }

  // ============================================================
  // Employee View tab
  // ============================================================
  var empSelectHidden = document.getElementById('roster-employee-selected');
  var empProfile = document.getElementById('roster-employee-profile');
  var empCalendar = document.getElementById('roster-employee-calendar');
  var empEmpty = document.getElementById('roster-employee-empty');
  if (empEmpty) empEmpty.hidden = R.employees.length > 0;

  function renderEmployeeCalendar(empNo) {
    var emp = empByNo[empNo];
    if (!emp || !empCalendar) return;
    var monthMatrix = R.matrix[empNo] || {};
    var allDays = [];
    R.weeks.forEach(function (w) { allDays = allDays.concat(w.days); });

    var counts = { Working: 0, 'Week Off': 0, 'Sick Leave': 0, 'Casual Leave': 0, 'On Leave': 0 };
    allDays.forEach(function (d) { var st = (monthMatrix[d] || {}).status; if (st) counts[st] = (counts[st] || 0) + 1; });

    if (empProfile) {
      empProfile.innerHTML = '<b>' + emp.name + '</b> (' + emp.emp_no + ') · ' + emp.role + ' · ' + emp.designation +
        '<span>Working ' + counts.Working + ' · Week off ' + counts['Week Off'] + ' · Sick ' + counts['Sick Leave'] +
        ' · Casual leave ' + counts['Casual Leave'] + ' · On leave ' + counts['On Leave'] + '</span>';
    }

    empCalendar.innerHTML = '';
    var dowLabels = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
    dowLabels.forEach(function (l) {
      var el = document.createElement('div');
      el.className = 'scr-cal-dow';
      el.textContent = l;
      empCalendar.appendChild(el);
    });
    var firstDow = new Date(allDays[0] + 'T00:00:00').getDay();
    for (var i = 0; i < firstDow; i++) empCalendar.appendChild(document.createElement('div'));

    allDays.forEach(function (day) {
      var rec = monthMatrix[day]; // undefined -- not "Working" -- for an unset day
      var d = new Date(day + 'T00:00:00');
      var cell = document.createElement('div');
      cell.className = 'scr-cal-cell' + (day === R.today ? ' is-today' : '');
      cell.innerHTML = '<span class="scr-cal-date">' + d.getDate() + '</span>' +
        (rec ? '<span class="scr-badge ' + statusBadgeClass(rec.status) + '"><i></i>' + statusText(rec) + '</span>' : '') +
        (rec && rec.status === 'Working' ? '<span class="scr-cal-shift">' + shiftLabel(rec) + '</span><span class="scr-cal-branch">' + (rec.branch || '') + '</span>' : '') +
        (isLeave(rec) ? '' : '<button type="button" class="scr-cal-edit" title="Edit day" data-cal-edit="' + day + '">' +
        '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h4l10-10a2.1 2.1 0 0 0-3-3L5 17z"></path></svg></button>');
      cell.addEventListener('click', function (e) {
        if (e.target.closest('[data-cal-edit]')) return;
        openDetailModal({
          title: emp.name + ' · ' + d.toLocaleDateString('en-US', { weekday: 'short' }) + ' ' + shortDate(day),
          sub: emp.role + ' · ' + emp.emp_no,
          badge: rec ? '<span class="scr-badge ' + statusBadgeClass(rec.status) + '"><i></i>' + statusText(rec) + '</span>' : '',
          fields: [
            ['Station', rec && rec.status === 'Working' ? rec.branch : ''],
            ['Shift 1', shiftLabel(rec)],
            ['Shift 2', rec && rec.shift2_start ? rec.shift2_start + ' – ' + rec.shift2_end : ''],
            ['Leave request', isLeave(rec) ? rec.leave_reference : ''],
          ],
          empty: rec ? 'No station or shift for this day.' : 'Nothing is rostered for this day.',
          note: isLeave(rec) ? 'Set by the approved leave request; it changes only if the leave is cancelled.' : '',
          action: isLeave(rec) ? null : { label: rec ? 'Edit day' : 'Set this day', run: function () { openEditDay(emp, day); } },
        });
      });
      if (cell.querySelector('[data-cal-edit]')) cell.querySelector('[data-cal-edit]').addEventListener('click', function (ev) {
        ev.stopPropagation();
        openEditDay(emp, day);
      });
      empCalendar.appendChild(cell);
    });
  }

  if (empSelectHidden) {
    // employee_key resolves to the employee's *name* (combo's combo_key), so
    // resolve the emp_no back out for the calendar/matrix lookups.
    document.addEventListener('change', function (e) {
      if (e.target !== empSelectHidden) return;
      var emp = empByName[empSelectHidden.value];
      if (emp) renderEmployeeCalendar(emp.emp_no);
    });
  }

  // ============================================================
  // Add Employee Roster drawer -- day-by-day builder in its "custom" slot
  // ============================================================
  (function () {
    var drawer = document.getElementById('drawerDutyRosterEmployee');
    if (!drawer) return;
    var host = drawer.querySelector('[data-days-host="days"]');
    var employeeHidden = drawer.querySelector('[data-field="employee_key"]');
    var weekSelect = drawer.querySelector('[data-field="week"]');
    if (!host || !employeeHidden || !weekSelect) return;

    // Two cards per row, not Branch Roster's one-row-scroll-sideways strip
    // (.scr-wk-grid): each card here holds a select and up to four time
    // inputs, so a whole week across in one line is too narrow to read even
    // in the wide drawer. Two wide columns that wrap keep every field
    // legible and cut a week of full-width blocks down to ~3-4 short rows.
    host.style.display = 'grid';
    host.style.gridTemplateColumns = 'repeat(2, minmax(0, 1fr))';
    host.style.gap = '10px';
    host.style.padding = '18px 20px';

    var branchOptionsHtml = '<option value="">Select branch</option>' +
      ALL_BRANCHES.map(function (b) { return '<option value="' + b.id + '">' + b.name + '</option>'; }).join('');

    function dayCard(emp, day, monthMatrix) {
      var rec = monthMatrix[day];
      var d = new Date(day + 'T00:00:00');
      if (isLeave(rec)) {
        // Approved leave: shown, not posted with the week.
        return '<div class="scr-wk-day-card" style="min-width:0" data-emp-day-leave="' + day + '">' +
          '<div class="scr-wk-day-head">' + d.toLocaleDateString('en-US', { weekday: 'long', day: 'numeric', month: 'short' }) + '</div>' +
          '<span class="scr-badge scr-badge-rejected"><i></i>' + statusText(rec) + '</span>' +
          '<div class="byky-help" style="margin-top:6px">Approved leave ' + (rec.leave_reference || '') + ' — cannot be changed here.</div></div>';
      }
      var dayType = rec ? (DAY_TYPE_VALUE[rec.status] || 'working') : '';
      var working = dayType === 'working';
      var branchId = rec && rec.branch ? (branchNameToId[rec.branch] || '') : '';
      var hasShift2 = !!(rec && rec.shift2_start);

      return '<div class="scr-wk-day-card" style="min-width:0" data-emp-day="' + day + '"' + (rec ? ' data-was-set' : '') + '>' +
        '<div class="scr-wk-day-head">' + d.toLocaleDateString('en-US', { weekday: 'long', day: 'numeric', month: 'short' }) + '</div>' +
        '<select class="byky-input byky-select" data-emp-day-type style="margin-bottom:6px">' +
        DAY_TYPE_OPTIONS.map(function (o) { return '<option value="' + o[0] + '"' + (o[0] === dayType ? ' selected' : '') + '>' + o[1] + '</option>'; }).join('') +
        '</select>' +
        '<div data-emp-day-working' + (working ? '' : ' hidden') + '>' +
        '<select class="byky-input byky-select" data-emp-day-branch style="margin-bottom:6px">' + branchOptionsHtml.replace('value="' + branchId + '"', 'value="' + branchId + '" selected') + '</select>' +
        '<label class="byky-help" style="display:block; margin-bottom:2px">Shift 1</label>' +
        '<input type="time" class="byky-input" data-emp-day-shift1-start value="' + ((rec && rec.shift1_start) || '') + '" style="margin-bottom:6px" />' +
        '<input type="time" class="byky-input" data-emp-day-shift1-end value="' + ((rec && rec.shift1_end) || '') + '" style="margin-bottom:6px" />' +
        '<label class="byky-switch-row" style="margin-bottom:6px"><input type="checkbox" class="byky-switch-input" data-emp-day-shift2-toggle' + (hasShift2 ? ' checked' : '') + ' /><span class="byky-switch"></span><span class="byky-switch-label">Add Shift 2</span></label>' +
        '<input type="time" class="byky-input" data-emp-day-shift2-start value="' + ((rec && rec.shift2_start) || '') + '"' + (hasShift2 ? '' : ' hidden') + ' style="margin-bottom:6px" />' +
        '<input type="time" class="byky-input" data-emp-day-shift2-end value="' + ((rec && rec.shift2_end) || '') + '"' + (hasShift2 ? '' : ' hidden') + ' />' +
        '</div></div>';
    }

    function renderDays() {
      var empName = employeeHidden.value;
      var emp = empByName[empName];
      var week = R.weeks[weekSelect.selectedIndex - 1]; // option 0 is the blank "Select"
      if (!emp || !week) {
        host.innerHTML = '<div class="scr-help">Choose an employee and a week above, then set each day below.</div>';
        return;
      }
      var monthMatrix = R.matrix[emp.emp_no] || {};
      host.innerHTML = week.days.map(function (day) { return dayCard(emp, day, monthMatrix); }).join('');
    }

    host.addEventListener('change', function (e) {
      var card = e.target.closest('[data-emp-day]');
      if (!card) return;
      if (e.target.matches('[data-emp-day-type]')) {
        card.querySelector('[data-emp-day-working]').hidden = e.target.value !== 'working';
      }
      if (e.target.matches('[data-emp-day-shift2-toggle]')) {
        var on = e.target.checked;
        card.querySelector('[data-emp-day-shift2-start]').hidden = !on;
        card.querySelector('[data-emp-day-shift2-end]').hidden = !on;
      }
      if (e.target.matches('[data-emp-day-branch]') && e.target.value) {
        var s1 = card.querySelector('[data-emp-day-shift1-start]');
        var s1e = card.querySelector('[data-emp-day-shift1-end]');
        if (s1 && !s1.value) {
          branchShiftDefaults(e.target.value, card.dataset.empDay).then(function (defaults) {
            if (defaults.shift1 && !s1.value) { s1.value = defaults.shift1.start; s1e.value = defaults.shift1.end; }
          });
        }
      }
    });

    employeeHidden.addEventListener('change', renderDays);
    weekSelect.addEventListener('change', renderDays);
    renderDays();

    drawer.addEventListener('click', function (e) {
      var button = e.target.closest('[data-scr-save]');
      if (!button) return;
      e.preventDefault();

      var empName = employeeHidden.value;
      var emp = empByName[empName];
      var week = R.weeks[weekSelect.selectedIndex - 1];
      if (!emp || !week) {
        Crud.showMessages({ title: 'Please fix the following', items: [{ field: 'Employee / Week', message: 'Choose both before saving.' }] });
        return;
      }

      var days = [];
      host.querySelectorAll('[data-emp-day]').forEach(function (card) {
        var dayType = card.querySelector('[data-emp-day-type]').value;
        // "Not set": an unset day is left alone; a day that was set is cleared.
        if (!dayType) {
          if (card.hasAttribute('data-was-set')) days.push({ date: card.dataset.empDay, day_type: 'not_set' });
          return;
        }
        var working = dayType === 'working';
        var hasShift2 = working && card.querySelector('[data-emp-day-shift2-toggle]').checked;
        days.push({
          date: card.dataset.empDay,
          day_type: dayType,
          branch: working ? (card.querySelector('[data-emp-day-branch]').value || null) : null,
          shift1_start: working ? (card.querySelector('[data-emp-day-shift1-start]').value || null) : null,
          shift1_end: working ? (card.querySelector('[data-emp-day-shift1-end]').value || null) : null,
          shift2_start: hasShift2 ? (card.querySelector('[data-emp-day-shift2-start]').value || null) : null,
          shift2_end: hasShift2 ? (card.querySelector('[data-emp-day-shift2-end]').value || null) : null,
        });
      });

      var again = button.dataset.scrSave === 'new';
      button.disabled = true;
      Crud.post(URLS.saveWeek, { employee: emp.id, days: days }).then(function (result) {
        button.disabled = false;
        if (result.body.ok) {
          if (again) { drawer.__bykyFill('add', null); renderDays(); Crud.toast(result.body.message); return; }
          Crud.toastAfterReload(result.body.message);
          window.location.reload();
          return;
        }
        Crud.showMessages({
          title: result.status === 403 ? 'Not allowed' : 'Please fix the following',
          subtitle: (result.body.errors || []).length > 1 ? result.body.errors.length + ' things need attention' : '',
          items: result.body.errors || [{ field: '', message: 'Something went wrong.' }],
        });
      });
    });
  })();

  // ============================================================
  // renders once on load
  // ============================================================
  renderStateTable();

  // ============================================================
  // Bulk Import (Excel) -- real read via SheetJS, real write via /import/
  // ============================================================
  (function () {
    if (typeof XLSX === 'undefined') return;
    var columnsEl = document.getElementById('roster-import-columns-data');
    var COLUMNS = columnsEl ? JSON.parse(columnsEl.textContent) : [];

    function sampleRows() {
      var sample = R.employees.slice(0, 3);
      var today = R.today;
      return sample.map(function (e) {
        var rec = (R.matrix[e.emp_no] || {})[today];
        return [e.emp_no, e.name, today, (rec && rec.status) || 'Working', (rec && rec.branch) || (ALL_BRANCHES[0] || {}).name || '',
          (rec && rec.shift1_start) || '09:00', (rec && rec.shift1_end) || '18:00', (rec && rec.shift2_start) || '', (rec && rec.shift2_end) || '', ''];
      });
    }

    function downloadSheet(rows, filename) {
      var ws = XLSX.utils.aoa_to_sheet([COLUMNS].concat(rows));
      var wb = XLSX.utils.book_new();
      XLSX.utils.book_append_sheet(wb, ws, 'Roster');
      XLSX.writeFile(wb, filename);
    }

    var templateBtn = document.getElementById('roster-download-template');
    if (templateBtn) templateBtn.addEventListener('click', function () { downloadSheet(sampleRows(), 'duty-roster-template.xlsx'); });

    var currentBtn = document.getElementById('roster-download-current');
    if (currentBtn) {
      currentBtn.addEventListener('click', function () {
        var rows = [];
        R.employees.forEach(function (e) {
          var monthMatrix = R.matrix[e.emp_no] || {};
          Object.keys(monthMatrix).forEach(function (day) {
            var rec = monthMatrix[day];
            rows.push([e.emp_no, e.name, day, rec.status, rec.branch || '', rec.shift1_start || '', rec.shift1_end || '', rec.shift2_start || '', rec.shift2_end || '', '']);
          });
        });
        if (!rows.length) { Crud.toast('Nothing rostered yet this month.'); return; }
        downloadSheet(rows, 'duty-roster-current-month.xlsx');
      });
    }

    var input = document.getElementById('roster-import-input');
    var filenameLabel = document.getElementById('roster-import-filename');
    var previewWrap = document.getElementById('roster-import-preview');
    var previewBody = document.getElementById('roster-import-preview-body');
    var applyBtn = document.getElementById('roster-import-apply');
    var cancelBtn = document.getElementById('roster-import-cancel');
    var parsedRows = [];

    function normalizeDate(v) {
      if (v instanceof Date) {
        return v.getFullYear() + '-' + String(v.getMonth() + 1).padStart(2, '0') + '-' + String(v.getDate()).padStart(2, '0');
      }
      var s = String(v == null ? '' : v).trim();
      var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s);
      return m ? m[0] : s;
    }

    if (input) {
      input.addEventListener('change', function () {
        var file = input.files && input.files[0];
        if (!file) return;
        if (filenameLabel) filenameLabel.textContent = file.name;
        var reader = new FileReader();
        reader.onload = function (e) {
          var wb = XLSX.read(e.target.result, { type: 'array', cellDates: true });
          var sheet = wb.Sheets[wb.SheetNames[0]];
          var json = XLSX.utils.sheet_to_json(sheet, { header: 1 });
          parsedRows = json.slice(1).filter(function (r) { return r && r.length && r[0]; });
          renderPreview();
        };
        reader.readAsArrayBuffer(file);
      });
    }

    function rowStatus(r) {
      var code = String(r[0] || '').trim();
      var emp = empByNo[code];
      if (!emp) return { ok: false, text: 'Unknown employee code' };
      var branchName = String(r[4] || '').trim();
      var dayType = DAY_TYPE_VALUE[String(r[3] || '').trim()] || String(r[3] || '').trim();
      if (dayType === 'working' && branchName && !branchNameToId[branchName]) return { ok: false, text: 'Unknown branch' };
      return { ok: true, text: 'Ready' };
    }

    function renderPreview() {
      if (!previewBody) return;
      previewBody.innerHTML = parsedRows.map(function (r, i) {
        var status = rowStatus(r);
        return '<tr data-import-row="' + i + '"><td>' + (i + 1) + '</td><td>' + (r[1] || r[0] || '') + '</td><td>' + normalizeDate(r[2]) + '</td><td>' + r[3] + '</td><td>' + (r[4] || '') + '</td>' +
          '<td data-import-result><span class="scr-badge ' + (status.ok ? 'scr-badge-pending' : 'scr-badge-rejected') + '"><i></i>' + status.text + '</span></td></tr>';
      }).join('');
      if (previewWrap) previewWrap.hidden = parsedRows.length === 0;
      if (applyBtn) { applyBtn.textContent = 'Apply to Roster'; applyBtn.onclick = null; applyBtn.disabled = false; }
    }

    function resetImport() {
      parsedRows = [];
      if (previewWrap) previewWrap.hidden = true;
      if (input) input.value = '';
      if (filenameLabel) filenameLabel.textContent = 'Import from Excel';
    }

    if (cancelBtn) cancelBtn.addEventListener('click', resetImport);

    function applyImport() {
      var rows = parsedRows.map(function (r) {
        var branchName = String(r[4] || '').trim();
        return {
          employee_code: String(r[0] || '').trim(),
          date: normalizeDate(r[2]),
          day_type: DAY_TYPE_VALUE[String(r[3] || '').trim()] || String(r[3] || '').trim(),
          branch: branchNameToId[branchName] || branchName || null,
          shift1_start: r[5] || null, shift1_end: r[6] || null,
          shift2_start: r[7] || null, shift2_end: r[8] || null,
          remarks: r[9] || '',
        };
      });

      applyBtn.disabled = true;
      Crud.post(URLS.import, { rows: rows }).then(function (result) {
        applyBtn.disabled = false;
        if (!result.body.ok) {
          Crud.showMessages({
            title: result.status === 403 ? 'Not allowed' : 'Could not import',
            items: result.body.errors || [{ field: '', message: 'Something went wrong.' }],
          });
          return;
        }
        (result.body.results || []).forEach(function (row) {
          var cell = previewBody.querySelector('[data-import-row="' + (row.row - 1) + '"] [data-import-result]');
          if (!cell) return;
          cell.innerHTML = '<span class="scr-badge ' + (row.ok ? 'scr-badge-approved' : 'scr-badge-rejected') + '"><i></i>' + (row.ok ? 'Saved' : row.message) + '</span>';
        });
        // The State/Branch/Employee tabs above still read the payload this
        // page loaded with -- rather than re-deriving it in JS, ask for a
        // reload, the one place this reload is a choice and not automatic
        // (the rows just written stay visible while that choice is made).
        applyBtn.textContent = 'Reload Page to See Changes';
        applyBtn.onclick = function () { window.location.reload(); };
        applyBtn.disabled = false;
      });
    }

    if (applyBtn) applyBtn.addEventListener('click', function () { if (applyBtn.onclick) return; applyImport(); });
  })();
})();

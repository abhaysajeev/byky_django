/* Targets screens (apps/targets, design/08-targets.md §6).

   Shared pieces do the heavy lifting: the drawer (byky-drawer.js), its save
   and the delete confirm (byky-crud.js). This file only:
     - submits the emirate filter on change,
     - makes a whole table row open its station,
     - Set Targets: the selection bar, and opening the drawer for the stations
       and month the page already shows (sent as hidden fields),
     - opens Edit from the row menu (the menu is moved out of its row while
       open, so the row it belongs to is the one byky-crud.js remembered),
     - refreshes a station's month figures every 60 seconds. */
(function () {
  'use strict';

  /* ── filter ── */
  document.addEventListener('change', function (e) {
    if (e.target.matches('[data-tgt-filter]')) e.target.form.submit();
  });

  /* ── whole-row links ── */
  document.addEventListener('click', function (e) {
    var row = e.target.closest('tr[data-href]');
    if (!row || e.target.closest('a, button, input, label')) return;
    if (e.metaKey || e.ctrlKey) { window.open(row.dataset.href, '_blank'); return; }
    window.location.href = row.dataset.href;
  });

  /* ── the drawer ── */
  function drawer() { return document.querySelector('[data-scr-name="target"]'); }

  function setField(d, name, value) {
    var el = d.querySelector('[data-field="' + name + '"]');
    if (el) el.value = value == null ? '' : value;
  }

  function show(d) {
    bootstrap.Offcanvas.getOrCreateInstance(d).show();
    setTimeout(function () {
      var amount = d.querySelector('[data-field="amount"]');
      if (amount) amount.focus();
    }, 250);
  }

  function openSet(ids, names, month, monthLabel) {
    var d = drawer();
    if (!d || !d.__bykyFill) return;
    d.__bykyFill('add', null);
    setField(d, 'branches', ids.join(','));
    setField(d, 'month', month);
    var who = names.length > 3 ? names.length + ' branches' : names.join(', ');
    setField(d, 'summary', who + ' · ' + monthLabel);
    show(d);
  }

  function openEdit(row) {
    var d = drawer();
    var node = row && document.getElementById(row.dataset.recordId);
    if (!d || !d.__bykyFill || !node) return;
    var record = JSON.parse(node.textContent);
    d.__bykyFill('edit', record);
    setField(d, 'summary', record.summary);
    show(d);
  }

  var page = document.querySelector('[data-tgt-manage]');

  document.addEventListener('click', function (e) {
    var set = e.target.closest('[data-tgt-set]');
    if (set) {
      e.preventDefault();
      openSet([set.dataset.branch], [set.dataset.name],
              set.dataset.month || (page && page.dataset.month),
              set.dataset.monthLabel || (page && page.dataset.monthLabel));
      return;
    }
    if (e.target.closest('[data-tgt-set-selected]') && page) {
      var picked = [].slice.call(page.querySelectorAll('[data-tgt-pick]:checked'));
      if (picked.length) {
        openSet(picked.map(function (b) { return b.value; }), picked.map(function (b) { return b.dataset.name; }),
                page.dataset.month, page.dataset.monthLabel);
      }
      return;
    }
    if (e.target.closest('[data-tgt-clear]') && page) {
      [].forEach.call(page.querySelectorAll('[data-tgt-pick], [data-tgt-all]'), function (b) { b.checked = false; });
      syncSelection();
    }
  });

  /* Edit from the row menu: handled here, before byky-drawer.js, because the
     open menu no longer sits inside its row. */
  document.addEventListener('click', function (e) {
    var edit = e.target.closest('[data-scr-open="target:edit"]');
    if (!edit) return;
    var menu = edit.closest('.scr-menu');
    var row = edit.closest('.scr-row') || (menu && menu.__bykyRow);
    e.preventDefault();
    e.stopPropagation();
    if (menu) menu.hidden = true;
    openEdit(row);
  }, true);

  /* ── Set Targets: selection ── */
  function syncSelection() {
    if (!page) return;
    var bar = page.querySelector('[data-tgt-selection]');
    if (!bar) return;
    var n = page.querySelectorAll('[data-tgt-pick]:checked').length;
    bar.hidden = n === 0;
    bar.querySelector('[data-tgt-selection-count]').textContent = n + ' selected';
  }
  document.addEventListener('change', function (e) {
    if (!page) return;
    if (e.target.matches('[data-tgt-all]')) {
      [].forEach.call(page.querySelectorAll('[data-tgt-pick]'), function (b) { b.checked = e.target.checked; });
    }
    if (e.target.matches('[data-tgt-all], [data-tgt-pick]')) syncSelection();
  });

  /* ── a station's month: refresh every 60 s ── */
  var month = document.querySelector('[data-tgt-month]');
  if (!month || !month.dataset.url) return;

  function put(name, text) {
    var el = document.querySelector('[data-tgt="' + name + '"]');
    if (el && text != null) el.textContent = text;
  }

  function refresh() {
    if (document.hidden) return;
    fetch(month.dataset.url, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        put('achieved', data.achieved);
        put('percent', data.percent + '%');
        put('remaining', data.remaining);
        put('days_left', data.days_left);
        var bar = document.querySelector('[data-tgt="bar"]');
        if (bar) bar.style.width = Math.max(0, Math.min(100, parseFloat(data.percent) || 0)) + '%';
        if (!data.today) return;
        put('today_target', data.today.target);
        var row = document.querySelector('[data-tgt-day="' + data.today.date + '"]');
        if (!row) return;
        row.querySelector('[data-col="achieved"]').textContent = data.today.achieved;
        var gap = row.querySelector('[data-col="gap"]');
        gap.textContent = data.today.gap;
        gap.classList.toggle('is-neg', data.today.gap.charAt(0) === '−');
        row.querySelector('[data-col="status"]').innerHTML =
          '<span class="scr-x-status is-' + data.today.status.key + '">' + data.today.status.label + '</span>';
      })
      .catch(function () { /* the next tick tries again */ });
  }

  setInterval(refresh, 60000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(); });
})();

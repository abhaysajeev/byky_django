/* Targets screens (apps/targets, design/08-targets.md).

   The drawer, its save and the delete confirm are the shared ones
   (byky-drawer.js, byky-crud.js). This file only:
     - submits the year / emirate / search filters,
     - opens Add prefilled from an empty month cell or the ticked stations,
     - hides the Stations field when a target is edited (a target's station
       and month are its identity; only amount and remarks change),
     - refreshes the month view's figures every 60 seconds. */
(function () {
  'use strict';

  /* ── filters ── */
  document.addEventListener('change', function (e) {
    if (e.target.matches('[data-tgt-filter]')) e.target.form.submit();
  });
  var search = document.querySelector('.scr-tgt-search');
  if (search) {
    var timer;
    search.addEventListener('input', function () {
      clearTimeout(timer);
      timer = setTimeout(function () { search.form.submit(); }, 450);
    });
  }

  /* ── the drawer ── */
  function drawer() { return document.querySelector('[data-scr-name="target"]'); }

  function stationsField(d) {
    var multi = d.querySelector('.byky-multi[data-field="branches"]');
    return multi ? (multi.closest('.byky-field') || multi.parentElement) : null;
  }

  function openAdd(branches, month) {
    var d = drawer();
    if (!d || !d.__bykyFill) return;
    d.__bykyFill('add', null);
    var field = stationsField(d);
    if (field) field.hidden = false;
    var multi = d.querySelector('.byky-multi[data-field="branches"]');
    if (multi) {
      [].forEach.call(multi.querySelectorAll('input[type="checkbox"]'), function (box) {
        box.checked = branches.indexOf(box.value) > -1;
      });
      if (multi.__bykyRenderPills) multi.__bykyRenderPills();
    }
    var select = d.querySelector('[data-field="month"]');
    if (select && month) { select.value = month; select.classList.toggle('has-value', !!select.value); }
    bootstrap.Offcanvas.getOrCreateInstance(d).show();
  }

  document.addEventListener('click', function (e) {
    var add = e.target.closest('[data-tgt-add]');
    if (add) {
      e.preventDefault();
      openAdd([add.dataset.branch], add.dataset.month);
      return;
    }
    if (e.target.closest('[data-tgt-for-selected]')) {
      var picked = [].map.call(document.querySelectorAll('[data-tgt-pick]:checked'), function (b) { return b.value; });
      if (picked.length) openAdd(picked, '');
      return;
    }
    var open = e.target.closest('[data-scr-open^="target:"]');
    if (open) {
      /* after byky-drawer.js has filled it */
      setTimeout(function () {
        var d = drawer(), field = d && stationsField(d);
        if (field) field.hidden = open.dataset.scrOpen === 'target:edit';
      }, 0);
    }
  });

  /* ── row selection ── */
  function syncSelected() {
    var button = document.querySelector('[data-tgt-for-selected]');
    if (!button) return;
    var n = document.querySelectorAll('[data-tgt-pick]:checked').length;
    button.disabled = n === 0;
    button.textContent = n ? 'Set for ' + n + ' selected' : 'Set for selected';
  }
  document.addEventListener('change', function (e) {
    if (e.target.matches('[data-tgt-all]')) {
      [].forEach.call(document.querySelectorAll('[data-tgt-pick]'), function (b) { b.checked = e.target.checked; });
    }
    if (e.target.matches('[data-tgt-all], [data-tgt-pick]')) syncSelected();
  });

  /* ── month view: refresh every 60 s ── */
  var month = document.querySelector('[data-tgt-month]');
  if (!month || !month.dataset.url) return;

  function set(sel, text) {
    var el = month.querySelector('[data-tgt="' + sel + '"]');
    if (el && text != null) el.textContent = text;
  }

  function refresh() {
    if (document.hidden) return;
    fetch(month.dataset.url, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        set('achieved', data.achieved);
        set('remaining', data.remaining);
        set('days_left', data.days_left);
        set('percent', data.percent + '%');
        var chip = month.querySelector('[data-tgt="percent"]');
        if (chip) chip.className = 'scr-tgt-chip is-' + data.tone;
        set('today_required', data.today ? data.today.required : '—');
        data.days.forEach(function (d) {
          var row = document.querySelector('[data-tgt-day="' + d.date + '"]');
          if (!row || d.state !== 'today') return;
          var got = row.querySelector('[data-col="achieved"]');
          if (got) got.innerHTML = (d.achieved || '—') + ' <small>so far</small>';
          var gap = row.querySelector('[data-col="gap"]');
          if (gap && d.gap != null) gap.innerHTML = '<span class="scr-tgt-gap">' + d.gap + '</span>';
        });
      })
      .catch(function () { /* the next tick tries again */ });
  }

  setInterval(refresh, 60000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(); });
})();

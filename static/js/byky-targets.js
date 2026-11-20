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
    var pageNode = document.querySelector('[data-tgt-manage]');
    var setBtn = document.querySelector('[data-tgt-set][data-kind]');
    setField(d, 'kind', (pageNode && pageNode.dataset.kind) || (setBtn && setBtn.dataset.kind) || 'sales');
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

/* Target Profiles: the add / edit page (design/11-target-profiles.md §6).
   The checkboxes of the Branches multi-select are the truth; the amount table
   follows them, keeping what was typed for a branch while it stays chosen. */
(function () {
  'use strict';

  var form = document.querySelector('[data-tgt-profile-form]');
  var source = document.getElementById('tgt-profile');
  if (!form || !source) return;
  var data = JSON.parse(source.textContent);
  var fyChoices = JSON.parse(document.getElementById('tgt-fy-choices').textContent);
  var crud = window.BykyCrud;
  var multi = document.getElementById('tp-branches');
  var amounts = {};                       // branch id -> typed amount
  var names = {};                         // branch id -> name
  (data.assignments || []).forEach(function (a) {
    if (!a.active) return;
    amounts[String(a.id)] = a.amount;
    names[String(a.id)] = a.name;
  });

  function field(name) { return form.querySelector('[data-tp="' + name + '"]'); }
  function type() { return field('period_type').value; }
  function isCustomers() { return field('metric').value === 'customers'; }

  /* ── details ── */
  ['name', 'metric', 'period_type', 'status', 'remarks'].forEach(function (k) {
    field(k).value = data[k] == null ? '' : data[k];
  });

  var HELP = {
    daily: 'One target per day, from the first day to the last. A single special day: pick the same day twice.',
    monthly: 'One target per calendar month, from the first month to the last.',
    custom: 'One target for the whole range — a campaign or a festival week.',
    annual: 'One target per financial year. Its amount is shared by the month weights below.'
  };
  var AMOUNT = { daily: 'per day', monthly: 'per month', custom: 'for the whole range', annual: 'per financial year' };

  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function lastOfMonth(ym) {                     // "2026-12" -> "2026-12-31"
    var p = ym.split('-').map(Number);
    return p[0] + '-' + pad(p[1]) + '-' + pad(new Date(p[0], p[1], 0).getDate());
  }
  function fyEnd(start) {
    var hit = fyChoices.filter(function (f) { return f.value === start; })[0];
    return hit ? hit.end : start;
  }
  function fyContaining(day) {
    var hit = fyChoices.filter(function (f) { return f.value <= day && day <= f.end; })[0];
    return hit ? hit.value : (fyChoices[0] && fyChoices[0].value);
  }
  function el(sel) { return form.querySelector(sel); }

  function setRange(from, to) {
    el('[data-tp-date="from"]').value = from;
    el('[data-tp-date="to"]').value = to;
    el('[data-tp-month="from"]').value = from.slice(0, 7);
    el('[data-tp-month="to"]').value = to.slice(0, 7);
    el('[data-tp-fy="from"]').value = fyContaining(from);
    el('[data-tp-fy="to"]').value = fyContaining(to);
  }
  function range() {
    var t = type();
    if (t === 'monthly') {
      var fm = el('[data-tp-month="from"]').value, tm = el('[data-tp-month="to"]').value;
      return [fm ? fm + '-01' : '', tm ? lastOfMonth(tm) : ''];
    }
    if (t === 'annual') {
      var ff = el('[data-tp-fy="from"]').value, tf = el('[data-tp-fy="to"]').value;
      return [ff, tf ? fyEnd(tf) : ''];
    }
    return [el('[data-tp-date="from"]').value, el('[data-tp-date="to"]').value];
  }

  function showAmountLabels() {
    var unit = isCustomers() ? 'customers' : 'AED';
    el('[data-tp-amount-head]').textContent = 'Target (' + unit + ', ' + (AMOUNT[type()] || '') + ')';
    el('[data-tp-amount-label]').textContent = 'Amount for all chosen (' + unit + ')';
    el('[data-tp-amount-help]').textContent = isCustomers()
      ? 'Customers: a whole number of unique customers, ' + (AMOUNT[type()] || '') + '.'
      : 'Sales: AED ' + (AMOUNT[type()] || '') + ', planned over the period\'s days.';
  }
  function showType() {
    var t = type();
    var kind = t === 'monthly' ? 'month' : (t === 'annual' ? 'fy' : 'date');
    [].forEach.call(form.querySelectorAll('[data-tp-range]'), function (n) { n.hidden = n.dataset.tpRange !== kind; });
    el('[data-tp-from-label]').textContent = t === 'daily' ? 'First day' : 'From';
    el('[data-tp-type-help]').textContent = HELP[t] || '';
    el('[data-tp-weights]').hidden = t !== 'annual';
    showAmountLabels();
  }

  /* ── the branches' amounts ── */
  function chosen() { return window.BykyMulti ? window.BykyMulti.values(multi) : []; }
  function nameOf(id) {
    if (names[id]) return names[id];
    var box = multi.querySelector('input[value="' + id + '"]');
    var label = box && box.parentElement.querySelector('span');
    return label ? label.textContent : id;
  }
  function drawAmounts() {
    var body = el('[data-tp-amount-rows]');
    var ids = chosen();
    body.innerHTML = '';
    ids.map(function (id) { return { id: id, name: nameOf(id) }; })
      .sort(function (a, b) { return a.name.localeCompare(b.name); })
      .forEach(function (b) {
        var tr = document.createElement('tr');
        var name = document.createElement('td');
        name.setAttribute('data-label', 'Branch');
        name.textContent = b.name;
        var cell = document.createElement('td');
        cell.className = 'num';
        cell.setAttribute('data-label', 'Target');
        var input = document.createElement('input');
        input.className = 'scr-drawer-input scr-tp-amount';
        input.inputMode = 'decimal';
        input.autocomplete = 'off';
        input.setAttribute('aria-label', 'Target for ' + b.name);
        input.value = amounts[b.id] || '';
        input.addEventListener('input', function () { amounts[b.id] = input.value; });
        cell.appendChild(input);
        tr.appendChild(name);
        tr.appendChild(cell);
        body.appendChild(tr);
      });
    el('[data-tp-amounts]').hidden = ids.length === 0;
  }
  multi.addEventListener('change', drawAmounts);
  el('[data-tp-apply]').addEventListener('click', function () {
    var value = el('[data-tp-apply-amount]').value.trim();
    if (!value) return;
    chosen().forEach(function (id) { amounts[id] = value; });
    drawAmounts();
  });

  /* ── month weights (annual) ── */
  var weights = el('[data-tp-weight-rows]');
  function total() {
    var sum = 0;
    [].forEach.call(weights.querySelectorAll('input'), function (n) { sum += parseFloat(n.value) || 0; });
    var rounded = Math.round(sum * 1000) / 1000;
    var out = el('[data-tp-weight-total]');
    out.textContent = rounded + '%';
    out.parentElement.classList.toggle('is-off', rounded !== 100);
  }
  function drawWeights(values) {
    weights.innerHTML = '';
    (data.month_names || []).forEach(function (name, i) {
      var label = document.createElement('label');
      label.className = 'scr-inc-field scr-tp-weight';
      var span = document.createElement('span');
      span.className = 'byky-label';
      span.textContent = name;
      var input = document.createElement('input');
      input.className = 'scr-drawer-input';
      input.inputMode = 'decimal';
      input.dataset.month = String(i + 1);
      input.value = String(Number(values[String(i + 1)] || 0));
      input.addEventListener('input', total);
      label.appendChild(span);
      label.appendChild(input);
      weights.appendChild(label);
    });
    total();
  }
  function readWeights() {
    var out = {};
    [].forEach.call(weights.querySelectorAll('input'), function (n) { out[n.dataset.month] = n.value.trim() || '0'; });
    return out;
  }
  el('[data-tp-weights-reset]').addEventListener('click', function () { drawWeights(data.default_weights); });

  /* ── save / delete ── */
  function busy(button, on) {
    if (!button) return;
    button.classList.toggle('is-saving', on);
    button.disabled = on;
  }
  function collect() {
    var r = range();
    return {
      pk: data.pk, name: field('name').value.trim(), metric: field('metric').value, period_type: type(),
      starts_on: r[0], ends_on: r[1], status: field('status').value, remarks: field('remarks').value.trim(),
      branches: chosen().map(function (id) { return { id: id, amount: amounts[id] || '' }; }),
      weights: type() === 'annual' ? readWeights() : null
    };
  }

  var save = document.querySelector('[data-tgt-profile-save]');
  if (save) {
    save.addEventListener('click', function () {
      busy(save, true);
      crud.post(form.dataset.saveUrl, collect()).then(function (res) {
        busy(save, false);
        var body = res.body || {};
        if (!body.ok) {
          crud.showMessages({ title: 'Please fix the following',
                              items: body.errors || [{ message: 'Something went wrong. Try again.' }] });
          return;
        }
        if (body.refused && body.refused.length) {
          /* saved, but some branches clash with another profile: say which, stay here */
          data.pk = body.pk;
          if (body.redirect) window.history.replaceState(null, '', body.redirect);
          crud.showMessages({
            title: 'Saved — some targets were not set',
            subtitle: 'A branch can\'t have two targets of the same type and metric over the same dates.',
            items: body.refused.map(function (r) { return { field: r.branch, message: r.reason }; })
          });
          return;
        }
        crud.toastAfterReload(body.message);
        window.location.href = body.redirect || window.location.href;
      }).catch(function () {
        busy(save, false);
        crud.showMessages({ title: 'Not saved', items: [{ message: 'The connection dropped. Try again.' }] });
      });
    });
  }

  var del = document.querySelector('[data-tgt-profile-delete]');
  if (del) {
    del.addEventListener('click', function () {
      crud.confirmThen({ title: 'Delete profile', body: 'Delete “' + del.dataset.name + '”? Its targets go with it.' },
        function () {
          crud.post(del.dataset.url, {}).then(function (res) {
            var body = res.body || {};
            if (!body.ok) { crud.showMessages({ title: 'Not deleted', items: body.errors || [] }); return; }
            crud.toastAfterReload(body.message);
            window.location.href = body.redirect;
          });
        });
    });
  }

  field('period_type').addEventListener('change', showType);
  field('metric').addEventListener('change', showAmountLabels);
  setRange(data.starts_on, data.ends_on);
  drawWeights(data.weights || data.default_weights);
  showType();
  drawAmounts();
})();

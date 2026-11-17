/* Incentive screens (apps/incentives, design/09-incentives.md section 7).
 *
 *  - Calculate / Recalculate a station's month; Approve a run (confirm first).
 *  - Schemes: Copy and Delete.
 *  - The scheme form: rows of components and variables, a click on a
 *    variable puts it in the formula last focused, every formula is checked
 *    as typed (the server's own check), Save, and Try it on a real day.
 * Writes post JSON and answer {ok, errors} like every other screen. */
(function () {
  'use strict';
  var crud = window.BykyCrud;
  if (!crud) return;

  function fail(title, body) {
    crud.showMessages({ title: title, items: (body && body.errors) || [{ message: 'Something went wrong. Try again.' }] });
  }

  function busy(button, on) {
    if (!button) return;
    button.classList.toggle('is-saving', on);
    button.disabled = on;
  }

  /* ── Calculate / Recalculate / Approve ───────────────────────────── */
  function calculate(button, url, branch, month, name, again) {
    var run = function () {
      busy(button, true);
      crud.post(url, { branch: branch, month: month }).then(function (res) {
        busy(button, false);
        if (!res.body.ok) { fail('Not calculated', res.body); return; }
        crud.toastAfterReload(res.body.message);
        window.location.href = res.body.url;
      });
    };
    if (!again) { run(); return; }
    crud.confirmThen({
      title: 'Recalculate ' + name + '?',
      body: 'The draft is worked out again from the roster and sales as they are now. Its lines are replaced.',
      action: 'Recalculate'
    }, run);
  }

  document.addEventListener('click', function (e) {
    var calc = e.target.closest('[data-inc-calc]');
    if (calc) {
      var table = calc.closest('[data-inc-calculate]');
      calculate(calc, table.dataset.url, calc.dataset.branch, table.dataset.month, calc.dataset.name,
                calc.textContent.trim() === 'Recalculate');
      return;
    }
    var recalc = e.target.closest('[data-inc-recalc]');
    if (recalc) {
      calculate(recalc, recalc.dataset.url, recalc.dataset.branch, recalc.dataset.month, recalc.dataset.name, true);
      return;
    }
    var approve = e.target.closest('[data-inc-approve]');
    if (approve) {
      crud.confirmThen({
        title: 'Approve ' + approve.dataset.name + '?',
        body: 'AED ' + approve.dataset.total + ' becomes final. An approved month can\'t be recalculated or changed.',
        action: 'Approve'
      }, function () {
        busy(approve, true);
        crud.post(approve.dataset.url, {}).then(function (res) {
          busy(approve, false);
          if (!res.body.ok) { fail('Not approved', res.body); return; }
          crud.toastAfterReload(res.body.message);
          window.location.reload();
        });
      });
    }
  });

  /* ── Schemes list: Copy / Delete ─────────────────────────────────── */
  var list = document.querySelector('[data-inc-schemes]');
  if (list) {
    list.addEventListener('click', function (e) {
      var row = e.target.closest('tr[data-pk]');
      if (!row) return;
      if (e.target.closest('[data-inc-copy]')) {
        crud.post(list.dataset.copyUrl.replace('/0/', '/' + row.dataset.pk + '/'), {}).then(function (res) {
          if (!res.body.ok) { fail('Not copied', res.body); return; }
          crud.toastAfterReload(res.body.message);
          window.location.href = res.body.url;
        });
      }
      if (e.target.closest('[data-inc-delete]')) {
        crud.confirmThen({ title: 'Delete “' + row.dataset.name + '”?', body: 'The scheme and its formulas go.' },
          function () {
            crud.post(list.dataset.deleteUrl.replace('/0/', '/' + row.dataset.pk + '/'), {}).then(function (res) {
              if (!res.body.ok) { fail('Not deleted', res.body); return; }
              crud.toastAfterReload(res.body.message);
              window.location.reload();
            });
          });
      }
    });
  }

  /* ── The scheme form ─────────────────────────────────────────────── */
  var form = document.querySelector('[data-inc-form]');
  if (!form) return;
  var locked = form.hasAttribute('data-locked');
  var data = JSON.parse(document.getElementById('inc-scheme').textContent);
  var applies = JSON.parse(document.getElementById('inc-applies').textContent);
  var lastFormula = null;

  function field(name) { return form.querySelector('[data-inc="' + name + '"]'); }

  ['name', 'effective_from', 'effective_to', 'pays_when', 'notes'].forEach(function (k) { field(k).value = data[k] || ''; });
  field('is_active').checked = !!data.is_active;
  form.querySelectorAll('[data-inc-stations] input').forEach(function (box) {
    box.checked = data.branches.indexOf(parseInt(box.value, 10)) >= 0;
  });

  function input(cls, value, label, attrs) {
    var el = document.createElement('input');
    el.className = 'scr-drawer-input' + (cls ? ' ' + cls : '');
    el.value = value || '';
    el.setAttribute('aria-label', label);
    el.autocomplete = 'off';
    el.spellcheck = false;
    Object.keys(attrs || {}).forEach(function (k) { el.setAttribute(k, attrs[k]); });
    return el;
  }

  function wrap(el, withCheck) {
    var box = document.createElement('div');
    box.className = 'scr-inc-field';
    box.appendChild(el);
    if (withCheck) {
      var note = document.createElement('span');
      note.className = 'scr-inc-check';
      note.setAttribute('aria-live', 'polite');
      box.appendChild(note);
    }
    return box;
  }

  function removeButton() {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'scr-inc-remove';
    b.setAttribute('aria-label', 'Remove this row');
    b.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"></path></svg>';
    b.addEventListener('click', function () { b.closest('.scr-inc-row').remove(); checkAll(); });
    return b;
  }

  function addComponent(c) {
    var row = document.createElement('div');
    row.className = 'scr-inc-row';
    row.dataset.kind = 'component';
    row.appendChild(wrap(input('', c.name, 'Component name', { 'data-k': 'name', placeholder: 'Name, e.g. Primary cashier', maxlength: '60' })));
    var select = document.createElement('select');
    select.className = 'scr-drawer-input';
    select.setAttribute('aria-label', 'Applies to');
    select.dataset.k = 'applies_to';
    applies.forEach(function (a) {
      var o = document.createElement('option');
      o.value = a.value; o.textContent = a.label; o.selected = a.value === c.applies_to;
      select.appendChild(o);
    });
    row.appendChild(wrap(select));
    row.appendChild(wrap(input('scr-inc-formula', c.formula, 'Amount formula', { 'data-k': 'formula', placeholder: 'e.g. basis * 1%' }), true));
    row.appendChild(removeButton());
    form.querySelector('[data-inc-rows="components"]').appendChild(row);
    return row;
  }

  function addVariable(v) {
    var row = document.createElement('div');
    row.className = 'scr-inc-row is-var';
    row.dataset.kind = 'variable';
    row.appendChild(wrap(input('scr-inc-formula', v.name, 'Variable name', { 'data-k': 'name', placeholder: 'name, e.g. rate', maxlength: '40' })));
    row.appendChild(wrap(input('scr-inc-formula', v.formula, 'Variable formula', { 'data-k': 'formula', placeholder: 'e.g. 1% / max(tier2, 2)' }), true));
    row.appendChild(removeButton());
    form.querySelector('[data-inc-rows="variables"]').appendChild(row);
    return row;
  }

  data.components.forEach(addComponent);
  data.variables.forEach(addVariable);
  form.addEventListener('click', function (e) {
    var add = e.target.closest('[data-inc-add]');
    if (!add) return;
    var row = add.dataset.incAdd === 'components'
      ? addComponent({ name: '', applies_to: 'primary', formula: '' }) : addVariable({ name: '', formula: '' });
    row.querySelector('input').focus();
  });

  function collect() {
    function rows(kind) {
      return Array.prototype.map.call(form.querySelectorAll('.scr-inc-row[data-kind="' + kind + '"]'), function (r) {
        var out = {};
        r.querySelectorAll('[data-k]').forEach(function (el) { out[el.dataset.k] = el.value.trim(); });
        return out;
      });
    }
    return {
      pk: data.pk, name: field('name').value.trim(), effective_from: field('effective_from').value,
      effective_to: field('effective_to').value, pays_when: field('pays_when').value.trim(),
      is_active: field('is_active').checked, notes: field('notes').value.trim(),
      branches: Array.prototype.map.call(form.querySelectorAll('[data-inc-stations] input:checked'), function (b) { return b.value; }),
      variables: rows('variable'), components: rows('component')
    };
  }

  /* As-you-type check: the server's own rules, so the page never disagrees with Save. */
  var timers = new WeakMap();
  function noteFor(el) {
    return el.dataset.inc === 'pays_when' ? form.querySelector('[data-check-for="pays_when"]')
      : el.closest('.scr-inc-field').querySelector('.scr-inc-check');
  }
  function check(el) {
    var note = noteFor(el);
    if (!note) return;
    if (!el.value.trim()) { note.textContent = ''; note.className = 'scr-inc-check'; return; }
    var names = collect().variables.map(function (v) { return v.name; }).filter(Boolean);
    crud.post(form.dataset.checkUrl, {
      formula: el.value, scope: el.dataset.scope === 'pays_when' ? 'pays_when' : 'any', variables: names
    }).then(function (res) {
      note.textContent = res.body.ok ? 'Looks right.' : res.body.message;
      note.className = 'scr-inc-check ' + (res.body.ok ? 'is-ok' : 'is-bad');
    });
  }
  function checkAll() { form.querySelectorAll('.scr-inc-formula[data-k="formula"], [data-inc="pays_when"]').forEach(check); }

  form.addEventListener('input', function (e) {
    var el = e.target;
    if (!(el.matches('[data-k="formula"]') || el.matches('[data-inc="pays_when"]'))) return;
    clearTimeout(timers.get(el));
    timers.set(el, setTimeout(function () { check(el); }, 350));
  });
  form.addEventListener('focusin', function (e) {
    if (e.target.matches('.scr-inc-formula')) lastFormula = e.target;
  });
  form.addEventListener('click', function (e) {
    var chip = e.target.closest('[data-inc-insert]');
    if (!chip) return;
    var el = lastFormula || form.querySelector('.scr-inc-row [data-k="formula"]');
    if (!el || locked) return;
    var name = chip.dataset.incInsert;
    var start = el.selectionStart == null ? el.value.length : el.selectionStart;
    var end = el.selectionEnd == null ? el.value.length : el.selectionEnd;
    el.value = el.value.slice(0, start) + name + el.value.slice(end);
    el.focus();
    el.setSelectionRange(start + name.length, start + name.length);
    el.dispatchEvent(new Event('input', { bubbles: true }));
  });

  if (locked) {
    form.querySelectorAll('input, select, [data-inc-add], .scr-inc-remove').forEach(function (el) {
      if (!el.closest('.scr-inc-try')) el.disabled = true;
    });
  } else {
    checkAll();
  }

  var save = document.querySelector('[data-inc-save]');
  if (save) {
    save.addEventListener('click', function () {
      busy(save, true);
      crud.post(form.dataset.saveUrl, collect()).then(function (res) {
        busy(save, false);
        if (!res.body.ok) { fail('Please fix the following', res.body); return; }
        crud.toastAfterReload(res.body.message);
        window.location.href = res.body.url;
      });
    });
  }

  /* Try it */
  var tryBtn = form.querySelector('[data-inc-try]');
  var out = form.querySelector('[data-inc-try-out]');
  function money(v) { return Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function text(tag, value, cls) { var el = document.createElement(tag); el.textContent = value; if (cls) el.className = cls; return el; }
  tryBtn.addEventListener('click', function () {
    var payload = collect();
    payload.branch = form.querySelector('[data-inc-try-branch]').value;
    payload.date = form.querySelector('[data-inc-try-date]').value;
    busy(tryBtn, true);
    crud.post(form.dataset.tryUrl, payload).then(function (res) {
      busy(tryBtn, false);
      out.innerHTML = '';
      if (!res.body.ok) { fail('Can\'t try it yet', res.body); return; }
      var b = res.body, v = b.values;
      var box = text('div', '', 'scr-inc-result');
      box.appendChild(text('p', b.date + (b.has_target ? (b.pays_now ? ' · the condition holds' : ' · the condition does not hold (yet)') : ' · no sales target this month'), 'scr-x-note'));
      var vals = text('div', '', 'scr-inc-values');
      [['achieved', 'Achieved'], ['target', 'Target'], ['work_days', 'Working days'], ['basis', 'Basis'], ['cashiers', 'Cashiers'], ['labour', 'Labour'], ['tier2', 'Tier 2']].forEach(function (pair) {
        var s = text('span', pair[1] + ' '); s.appendChild(text('b', v[pair[0]])); vals.appendChild(s);
      });
      box.appendChild(vals);
      if (!b.people.length) {
        box.appendChild(text('p', b.has_roster ? 'Nobody gets anything that day.' : 'Nobody is rostered Working there that day.', 'scr-x-note'));
      } else {
        var table = document.createElement('table');
        table.className = 'scr-x-table';
        var head = table.createTHead().insertRow();
        ['Employee', 'Role', 'Components', 'Amount'].forEach(function (h, i) { var th = document.createElement('th'); th.textContent = h; if (i === 3) th.className = 'num'; head.appendChild(th); });
        var body = table.createTBody();
        b.people.forEach(function (p) {
          var r = body.insertRow();
          r.insertCell().textContent = p.name;
          r.insertCell().textContent = p.role;
          r.insertCell().textContent = p.components.map(function (c) { return c[0] + ' ' + money(c[1]); }).join(', ') || '—';
          var a = r.insertCell(); a.className = 'num'; a.textContent = money(p.amount);
        });
        var wrapTable = text('div', '', 'scr-table-scroll');
        wrapTable.appendChild(table);
        box.appendChild(wrapTable);
      }
      out.appendChild(box);
    });
  });
})();

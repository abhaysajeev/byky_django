/*
 * Configurations (apps/company/configurations.py): the round-off steps follow
 * the decimals chosen; Add / Remove country rows; Save posts the whole page.
 */
(function () {
  'use strict';

  var root = document.querySelector('[data-config]');
  if (!root) return;

  function json(id) {
    var el = document.getElementById(id);
    try { return el ? JSON.parse(el.textContent) : null; } catch (e) { return null; }
  }
  var steps = json('config-steps') || {};
  var countries = json('config-countries') || [];
  var body = root.querySelector('[data-config-rows]');
  var empty = root.querySelector('[data-config-empty]');
  function crud() { return window.BykyCrud || {}; }

  function fillSteps(row) {
    var places = row.querySelector('[data-f="decimal_places"]').value;
    var select = row.querySelector('[data-f="round_off_step"]');
    var wanted = select.value || select.dataset.value || '';
    var list = steps[places] || [];
    // Keep the same amount when the decimals change (0.25 -> 0.250).
    var match = list.filter(function (s) { return Number(s) === Number(wanted); })[0];
    select.innerHTML = list.map(function (s) {
      return '<option value="' + s + '">' + s + '</option>';
    }).join('');
    select.value = match || (list.indexOf(places === '2' ? '0.25' : '0.250') > -1 ? (places === '2' ? '0.25' : '0.250') : list[0]);
  }

  function refreshEmpty() {
    if (empty) empty.hidden = body.querySelectorAll('[data-config-row]').length > 0;
  }

  body.querySelectorAll('[data-config-row]').forEach(fillSteps);
  body.addEventListener('change', function (e) {
    if (e.target.matches('[data-f="decimal_places"]')) fillSteps(e.target.closest('[data-config-row]'));
  });
  body.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-config-remove]');
    if (!btn) return;
    btn.closest('[data-config-row]').remove();
    refreshEmpty();
  });

  var add = root.querySelector('[data-config-add]');
  if (add) add.addEventListener('click', function () {
    var template = body.querySelector('[data-config-row]');
    var row;
    if (template) {
      row = template.cloneNode(true);
    } else {
      row = document.createElement('tr');
      row.setAttribute('data-config-row', '');
      row.innerHTML =
        '<td><select class="scr-input" data-f="country" aria-label="Country"></select></td>' +
        '<td><select class="scr-input" data-f="decimal_places" aria-label="Decimal places">' +
        '<option value="2">2 (0.00)</option><option value="3" selected>3 (0.000)</option></select></td>' +
        '<td><select class="scr-input" data-f="round_off_mode" aria-label="Round-off">' +
        '<option value="nearest">Nearest</option><option value="upward" selected>Upward</option>' +
        '<option value="downward">Downward</option></select></td>' +
        '<td><select class="scr-input" data-f="round_off_step" aria-label="Round-off step"></select></td>' +
        '<td><div class="scr-row-actions"><button type="button" class="scr-btn-secondary" data-config-remove>Remove</button></div></td>';
      row.querySelector('[data-f="country"]').innerHTML = countries.map(function (c) {
        return '<option value="' + c.id + '">' + String(c.name).replace(/</g, '&lt;') + '</option>';
      }).join('');
    }
    // The first country not already listed.
    var used = Array.prototype.map.call(body.querySelectorAll('[data-f="country"]'), function (s) { return s.value; });
    var free = countries.filter(function (c) { return used.indexOf(String(c.id)) === -1; })[0];
    if (free) row.querySelector('[data-f="country"]').value = String(free.id);
    row.querySelector('[data-f="round_off_step"]').dataset.value = '';
    row.querySelector('[data-f="round_off_step"]').value = '';
    body.appendChild(row);
    fillSteps(row);
    refreshEmpty();
  });

  /* -- primary dial codes: search, add as a chip, remove (owner, 12 Nov 2026) -- */
  var dialOptions = json('config-dial-codes') || [];
  var chosenIso = json('config-primary-dial-codes') || [];
  var chips = root.querySelector('[data-dial-chips]');
  var dialEmpty = root.querySelector('[data-dial-empty]');
  var picker = root.querySelector('[data-dial-picker]');
  var canEdit = !!root.dataset.canUpdate;
  var byIso = {};
  dialOptions.forEach(function (d) { byIso[d.iso2] = d; });

  function chosen() {
    return chips ? Array.prototype.map.call(chips.querySelectorAll('[data-iso]'), function (c) { return c.dataset.iso; }) : [];
  }

  function renumber() {
    if (!chips) return;
    chips.querySelectorAll('[data-iso]').forEach(function (chip, n) {
      chip.querySelector('.scr-dial-pos').textContent = String(n + 1);
    });
    if (dialEmpty) dialEmpty.hidden = chips.children.length > 0;
  }

  function addChip(d) {
    var chip = document.createElement('span');
    chip.className = 'scr-dial-chip';
    chip.dataset.iso = d.iso2;
    var pos = document.createElement('span');
    pos.className = 'scr-dial-pos';
    chip.appendChild(pos);
    chip.appendChild(document.createTextNode(d.flag + ' ' + d.country + ' '));
    var num = document.createElement('span');
    num.className = 'scr-dial-num';
    num.textContent = '+' + d.dial_code;
    chip.appendChild(num);
    if (canEdit) {
      var x = document.createElement('button');
      x.type = 'button';
      x.setAttribute('aria-label', 'Remove ' + d.country);
      x.textContent = '×';
      x.addEventListener('click', function () { chip.remove(); renumber(); });
      chip.appendChild(x);
    }
    chips.appendChild(chip);
    renumber();
  }

  if (chips) chosenIso.forEach(function (iso) { if (byIso[iso]) addChip(byIso[iso]); });
  renumber();

  if (picker) {
    var input = picker.querySelector('input');
    var list = picker.querySelector('.scr-combo-list');

    var matches = function () {
      var q = input.value.trim().toLowerCase().replace(/^\+/, '');
      var taken = chosen();
      return dialOptions.filter(function (d) {
        if (taken.indexOf(d.iso2) !== -1) return false;
        if (!q) return true;
        return d.country.toLowerCase().indexOf(q) !== -1 || d.dial_code.indexOf(q) === 0
          || d.iso2.toLowerCase() === q;
      }).slice(0, 30);
    };

    var draw = function () {
      list.innerHTML = '';
      matches().forEach(function (d) {
        var item = document.createElement('div');
        item.className = 'scr-combo-item';
        var main = document.createElement('span');
        main.className = 'scr-combo-item-main';
        main.textContent = d.flag + ' ' + d.country;
        var sub = document.createElement('span');
        sub.className = 'scr-combo-item-sub';
        sub.textContent = '+' + d.dial_code;
        item.appendChild(main);
        item.appendChild(sub);
        item.addEventListener('mousedown', function (e) {
          e.preventDefault();
          addChip(d);
          input.value = '';
          draw();
        });
        list.appendChild(item);
      });
      list.hidden = !list.children.length;
    };

    input.addEventListener('input', draw);
    input.addEventListener('focus', draw);
    input.addEventListener('blur', function () { list.hidden = true; });
    input.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      var first = matches()[0];
      if (first) { addChip(first); input.value = ''; draw(); }
    });
  }

  /* -- Save, with a visible state: spinner, then "Saved" ------------------------- */
  var MIN_BUSY_MS = 400;
  var SAVED_MS = 2000;

  function setButton(btn, state) {
    var label = btn.querySelector('[data-config-save-label]');
    btn.classList.toggle('is-saving', state === 'saving');
    btn.classList.toggle('is-saved', state === 'saved');
    btn.disabled = state === 'saving';
    btn.setAttribute('aria-busy', state === 'saving' ? 'true' : 'false');
    if (label) label.textContent = state === 'saving' ? 'Saving…' : state === 'saved' ? 'Saved ✓' : 'Save';
  }

  var save = root.querySelector('[data-config-save]');
  if (save) save.addEventListener('click', function () {
    if (save.disabled || !crud().post) return;
    var money = Array.prototype.map.call(body.querySelectorAll('[data-config-row]'), function (row) {
      function v(f) { return row.querySelector('[data-f="' + f + '"]').value; }
      return { country: v('country'), decimal_places: v('decimal_places'), round_off_mode: v('round_off_mode'),
               round_off_step: v('round_off_step') };
    });
    setButton(save, 'saving');
    var started = Date.now();
    var payload = {
      company: root.dataset.company,
      report_day_start: root.querySelector('[data-config-day-start]').value,
      money: money
    };
    if (chips) payload.primary_dial_codes = chosen();
    // never flash: the spinner stays at least MIN_BUSY_MS
    function after(fn) { setTimeout(fn, Math.max(0, MIN_BUSY_MS - (Date.now() - started))); }
    crud().post(root.dataset.saveUrl, payload).then(function (res) {
      after(function () {
        var data = res.body || {};
        if (data.ok) {
          setButton(save, 'saved');
          if (crud().toast) crud().toast(data.message || 'Saved.');
          setTimeout(function () { setButton(save, 'idle'); }, SAVED_MS);
          return;
        }
        setButton(save, 'idle');
        if (crud().showMessages) crud().showMessages({ title: 'Could not save', items: data.errors || [] });
      });
    }).catch(function () {
      after(function () {
        setButton(save, 'idle');
        if (crud().showMessages) crud().showMessages({ title: 'Could not save', items: [{ field: '', message: 'Could not reach the server. Try again.' }] });
      });
    });
  });
})();

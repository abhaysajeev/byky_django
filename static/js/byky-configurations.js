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

  var save = root.querySelector('[data-config-save]');
  if (save) save.addEventListener('click', function () {
    if (save.disabled || !crud().post) return;
    var money = Array.prototype.map.call(body.querySelectorAll('[data-config-row]'), function (row) {
      function v(f) { return row.querySelector('[data-f="' + f + '"]').value; }
      return { country: v('country'), decimal_places: v('decimal_places'), round_off_mode: v('round_off_mode'),
               round_off_step: v('round_off_step') };
    });
    save.disabled = true;
    crud().post(root.dataset.saveUrl, {
      company: root.dataset.company,
      report_day_start: root.querySelector('[data-config-day-start]').value,
      money: money
    }).then(function (res) {
      save.disabled = false;
      var data = res.body || {};
      if (data.ok) {
        if (crud().toast) crud().toast(data.message || 'Saved.');
        return;
      }
      if (crud().showMessages) crud().showMessages({ title: 'Could not save', items: data.errors || [] });
    }).catch(function () {
      save.disabled = false;
      if (crud().showMessages) crud().showMessages({ title: 'Could not save', items: [{ field: '', message: 'Could not reach the server. Try again.' }] });
    });
  });
})();

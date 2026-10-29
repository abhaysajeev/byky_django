/* byky-device-mapping.js -- Device Mapping's Re-map, Close mapping and the
 * transaction letter.
 *
 * Transaction No Prefix (owner, 28 Oct 2026): the chosen station's order
 * prefix, read only, and -- only at a multi-device station -- a one-letter
 * box: DUP - [A], so the tablet numbers its bookings DUP-A-1. The stations'
 * prefixes and multi-device flags come from branch-flags-data.
 *
 * Add goes through the shared drawer (byky-crud.js posts it). Re-map and
 * Close are per-device actions in the row menus, which byky-screen.js moves
 * out of the table -- so each button carries its device as data-* attributes
 * and clicks are caught at the document. The rules are server-side
 * (apps/devices/services.py: map_device, remap_device, close_mapping).
 */
(function () {
  'use strict';

  var root = document.querySelector('[data-device-mapping]');
  if (!root) return;

  function crud() { return window.BykyCrud || {}; }

  function urlFor(kind, pk) {
    var key = { remap: 'urlRemap', close: 'urlClose', letter: 'urlLetter' }[kind];
    return root.dataset[key].replace(/\/0\/([a-z]+)\/$/, '/' + pk + '/$1/');
  }

  var flags = {};
  try {
    JSON.parse(document.getElementById('branch-flags-data').textContent).forEach(function (b) {
      flags[String(b.id)] = b;
    });
  } catch (e) { /* no stations: nothing to show */ }

  function flagFor(stationId) { return flags[String(stationId || '')] || null; }

  // One capital letter, typed in any case.
  document.addEventListener('input', function (e) {
    var box = e.target;
    if (!box.matches('[data-field="transaction_letter"], [data-remap-letter], [data-letter-box]')) return;
    box.value = box.value.replace(/[^a-z]/gi, '').slice(0, 1).toUpperCase();
  });

  /* ── Add drawer: prefix + letter follow the chosen station ────────── */

  var drawer = document.getElementById('drawerDeviceMapping');

  function syncDrawer() {
    if (!drawer) return;
    var station = drawer.querySelector('[data-field="branch"]');
    var prefix = drawer.querySelector('[data-field="transaction_prefix"]');
    var letter = drawer.querySelector('[data-field="transaction_letter"]');
    if (!station || !prefix || !letter) return;
    var flag = flagFor(station.value);
    prefix.value = flag ? flag.prefix : '';
    letter.setAttribute('maxlength', '1');
    var wrap = letter.closest('.byky-field');
    var multi = !!(flag && flag.multi_device);
    if (wrap) wrap.hidden = !multi;
    if (!multi) letter.value = '';
  }

  if (drawer) {
    drawer.addEventListener('change', function (e) {
      if (e.target.matches('[data-field="branch"]')) syncDrawer();
    });
    drawer.addEventListener('shown.bs.offcanvas', syncDrawer);
    syncDrawer();
  }

  function show(name, on) {
    var box = document.querySelector('[data-scr-modal="' + name + '"]');
    var veil = document.querySelector('[data-scr-modal-veil="' + name + '"]');
    if (box) box.hidden = !on;
    if (veil) veil.hidden = !on;
    return box;
  }

  function send(button, url, payload, modal) {
    if (button.disabled || !crud().post) return;
    button.disabled = true;
    crud().post(url, payload).then(function (res) {
      var body = res.body || {};
      if (body.ok) {
        show(modal, false);
        if (crud().toastAfterReload) crud().toastAfterReload(body.message || 'Saved.');
        window.location.reload();
        return;
      }
      button.disabled = false;
      if (crud().showMessages) {
        crud().showMessages({ title: 'Could not save', items: body.errors || [{ field: '', message: 'Something went wrong.' }] });
      }
    }).catch(function () {
      button.disabled = false;
      if (crud().showMessages) {
        crud().showMessages({ title: 'Could not save', items: [{ field: '', message: 'Could not reach the server. Try again.' }] });
      }
    });
  }

  // Re-map and Close log out whoever is on the device (services.remap_device,
  // close_mapping); the modal says so when the row is in use.
  function warnLogout(modal, d) {
    var line = modal.querySelector('[data-mapping-logout]');
    if (line) line.hidden = !d.online;
  }

  function label(d) { return d.name ? d.name + ' (' + d.number + ')' : 'Device ' + d.number; }

  /* ── Re-map ───────────────────────────────────────────────────────── */

  var remap = document.querySelector('[data-scr-modal="device-remap"]');
  var station = remap && remap.querySelector('[data-remap-station]');
  var remapGo = remap && remap.querySelector('[data-remap-go]');
  var remapTx = remap && remap.querySelector('[data-remap-tx]');
  var remapLetter = remap && remap.querySelector('[data-remap-letter]');
  var current = null;

  function syncRemap() {
    if (!remapTx) return;
    var flag = flagFor(station.value);
    remap.querySelector('[data-remap-prefix]').value = flag ? flag.prefix : '';
    remapTx.hidden = !(flag && flag.multi_device);
    if (remapTx.hidden) remapLetter.value = '';
  }

  if (station) station.addEventListener('change', syncRemap);

  function openRemap(d) {
    current = d;
    remap.querySelector('[data-remap-title]').textContent = 'Re-map ' + label(d);
    remap.querySelector('[data-remap-sub]').textContent = 'Now at ' + (d.station || '—') + (d.from ? ' since ' + d.from : '') + '.';
    station.value = '';
    // The current station is not a destination.
    Array.prototype.forEach.call(station.options, function (opt) {
      if (!opt.value) return;
      opt.hidden = opt.disabled = opt.value === String(d.stationId);
    });
    remapGo.disabled = false;
    if (remapLetter) remapLetter.value = '';
    syncRemap();
    warnLogout(remap, d);
    show('device-remap', true);
  }

  if (remapGo) remapGo.addEventListener('click', function () {
    if (!current) return;
    send(remapGo, urlFor('remap', current.pk),
         { branch: station.value || null, transaction_letter: remapLetter ? remapLetter.value : '' }, 'device-remap');
  });

  /* ── Close mapping ────────────────────────────────────────────────── */

  var unmap = document.querySelector('[data-scr-modal="device-unmap"]');
  var unmapGo = unmap && unmap.querySelector('[data-unmap-go]');

  function openUnmap(d) {
    current = d;
    unmap.querySelector('[data-unmap-title]').textContent = 'Close mapping for ' + label(d) + '?';
    unmap.querySelector('[data-unmap-sub]').textContent = 'At ' + (d.station || '—') + (d.from ? ' since ' + d.from : '') + '.';
    unmapGo.disabled = false;
    warnLogout(unmap, d);
    show('device-unmap', true);
  }

  if (unmapGo) unmapGo.addEventListener('click', function () {
    if (!current) return;
    send(unmapGo, urlFor('close', current.pk), {}, 'device-unmap');
  });

  /* ── Set transaction letter (open mapping, in place) ──────────────── */

  var letterModal = document.querySelector('[data-scr-modal="device-letter"]');
  var letterBox = letterModal && letterModal.querySelector('[data-letter-box]');
  var letterGo = letterModal && letterModal.querySelector('[data-letter-go]');

  function openLetter(d) {
    current = d;
    letterModal.querySelector('[data-letter-title]').textContent = 'Transaction letter for ' + label(d);
    letterModal.querySelector('[data-letter-sub]').textContent = 'At ' + (d.station || '—') + '.';
    letterModal.querySelector('[data-letter-prefix]').value = d.prefix || '';
    letterBox.value = d.letter || '';
    letterGo.disabled = false;
    show('device-letter', true);
    letterBox.focus();
  }

  if (letterGo) letterGo.addEventListener('click', function () {
    if (!current) return;
    send(letterGo, urlFor('letter', current.pk), { transaction_letter: letterBox.value }, 'device-letter');
  });

  /* ── the menu buttons ─────────────────────────────────────────────── */

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-mapping-action]');
    if (!btn || btn.disabled) return;
    e.preventDefault();
    var menu = btn.closest('.scr-menu');
    if (menu) menu.hidden = true;
    var d = {
      pk: btn.dataset.pk, number: btn.dataset.number, name: btn.dataset.name,
      station: btn.dataset.station, stationId: btn.dataset.stationId, from: btn.dataset.from,
      online: btn.dataset.online === 'yes', prefix: btn.dataset.prefix, letter: btn.dataset.letter
    };
    if (btn.dataset.mappingAction === 'remap' && remap) openRemap(d);
    if (btn.dataset.mappingAction === 'close' && unmap) openUnmap(d);
    if (btn.dataset.mappingAction === 'letter' && letterModal) openLetter(d);
  });
})();

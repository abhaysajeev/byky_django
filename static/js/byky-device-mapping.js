/* byky-device-mapping.js -- Device Mapping's Re-map and Close mapping.
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
    return root.dataset[kind === 'remap' ? 'urlRemap' : 'urlClose'].replace(/\/0\/([a-z]+)\/$/, '/' + pk + '/$1/');
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

  function label(d) { return d.name ? d.name + ' (' + d.number + ')' : 'Device ' + d.number; }

  /* ── Re-map ───────────────────────────────────────────────────────── */

  var remap = document.querySelector('[data-scr-modal="device-remap"]');
  var station = remap && remap.querySelector('[data-remap-station]');
  var remapGo = remap && remap.querySelector('[data-remap-go]');
  var current = null;

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
    show('device-remap', true);
  }

  if (remapGo) remapGo.addEventListener('click', function () {
    if (!current) return;
    send(remapGo, urlFor('remap', current.pk), { branch: station.value || null }, 'device-remap');
  });

  /* ── Close mapping ────────────────────────────────────────────────── */

  var unmap = document.querySelector('[data-scr-modal="device-unmap"]');
  var unmapGo = unmap && unmap.querySelector('[data-unmap-go]');

  function openUnmap(d) {
    current = d;
    unmap.querySelector('[data-unmap-title]').textContent = 'Close mapping for ' + label(d) + '?';
    unmap.querySelector('[data-unmap-sub]').textContent = 'At ' + (d.station || '—') + (d.from ? ' since ' + d.from : '') + '.';
    unmapGo.disabled = false;
    show('device-unmap', true);
  }

  if (unmapGo) unmapGo.addEventListener('click', function () {
    if (!current) return;
    send(unmapGo, urlFor('close', current.pk), {}, 'device-unmap');
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
      station: btn.dataset.station, stationId: btn.dataset.stationId, from: btn.dataset.from
    };
    if (btn.dataset.mappingAction === 'remap' && remap) openRemap(d);
    if (btn.dataset.mappingAction === 'close' && unmap) openUnmap(d);
  });
})();

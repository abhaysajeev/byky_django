/* byky-device-settings.js -- Device Settings' logo, Copy to other stations,
 * Deactivate, and the drawer's two conveniences.
 *
 * Add and Edit go through the shared drawer (byky-crud.js posts them). The
 * rules are server-side (apps/devices/services.py: save_settings, set_logo,
 * apply_settings_to, deactivate_settings); this file only asks, prefills and
 * uploads.
 */
(function () {
  'use strict';

  var root = document.querySelector('[data-device-settings]');
  if (!root) return;

  function crud() { return window.BykyCrud || {}; }

  function show(name, on) {
    var box = document.querySelector('[data-scr-modal="' + name + '"]');
    var veil = document.querySelector('[data-scr-modal-veil="' + name + '"]');
    if (box) box.hidden = !on;
    if (veil) veil.hidden = !on;
  }

  function fail(items) {
    if (crud().showMessages) crud().showMessages({ title: 'Could not save', items: items });
  }

  function done(body, modal) {
    show(modal, false);
    if (crud().toastAfterReload) crud().toastAfterReload(body.message || 'Saved.');
    window.location.reload();
  }

  function send(button, url, payload, modal) {
    button.disabled = true;
    crud().post(url, payload).then(function (res) {
      var body = res.body || {};
      if (body.ok) return done(body, modal);
      button.disabled = false;
      fail(body.errors || [{ field: '', message: 'Something went wrong.' }]);
    }).catch(function () {
      button.disabled = false;
      fail([{ field: '', message: 'Could not reach the server. Try again.' }]);
    });
  }

  function urlFor(key, pk) {
    return root.dataset[key].replace('/0/', '/' + pk + '/');
  }

  /* ── the drawer: add for one station, and Copy from ───────────────── */

  var drawer = document.querySelector('[data-scr-name="device_settings"]');
  var sources = (function () {
    var node = document.getElementById('settings-source-data');
    try { return node ? JSON.parse(node.textContent) : {}; } catch (e) { return {}; }
  })();

  function field(id) { return drawer && drawer.querySelector('[data-field="' + id + '"]'); }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-settings-add]');
    if (!btn || !drawer) return;
    e.preventDefault();
    if (drawer.__bykyFill) drawer.__bykyFill('add', null);
    var branch = field('branch');
    if (branch) {
      branch.disabled = false;
      branch.value = btn.dataset.branch || '';
      fillHeaderFromStation();
    }
    if (window.bootstrap && bootstrap.Offcanvas) {
      bootstrap.Offcanvas.getOrCreateInstance(drawer).show();
    }
  });

  /* Header 1 is the station as the receipt names it, and it starts as the
     station's own name -- which is what 74 of the client's 95 live rows hold.
     Only ever filled while it is empty, so nobody's wording is overwritten. */
  function fillHeaderFromStation() {
    var branch = field('branch');
    var header = field('header_1');
    if (!branch || !header || header.value.trim()) return;
    var option = branch.options[branch.selectedIndex];
    if (option && option.value) header.value = option.textContent.trim();
  }

  if (drawer) {
    drawer.addEventListener('change', function (e) {
      if (!e.target.matches('[data-field]')) return;
      if (e.target.dataset.field === 'branch') fillHeaderFromStation();
      if (e.target.dataset.field === 'copy_from') copyFrom(e.target.value);
    });
  }

  function copyFrom(pk) {
    var values = sources[pk];
    if (!values) return;
    Object.keys(values).forEach(function (key) {
      // Header 1 and 2 name the station itself, so they are never copied.
      if (key === 'header_1' || key === 'header_2') return;
      var el = field(key);
      if (!el) return;
      if (el.type === 'checkbox') el.checked = !!values[key];
      else el.value = values[key] === null ? '' : values[key];
    });
    if (crud().toast) crud().toast('Filled from that station. Check the wording before saving.');
  }

  /* ── Receipt logo ─────────────────────────────────────────────────── */

  var logoModal = document.querySelector('[data-scr-modal="settings-logo"]');
  var logoFile = logoModal && logoModal.querySelector('[data-logo-file]');
  var logoGo = logoModal && logoModal.querySelector('[data-logo-go]');
  var logoRemove = logoModal && logoModal.querySelector('[data-logo-remove]');
  var logoWarnings = logoModal && logoModal.querySelector('[data-logo-warnings]');
  var current = null;

  function kb(bytes) { return Math.max(1, Math.round(Number(bytes || 0) / 1024)) + ' KB'; }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-settings-logo]');
    if (!btn || !logoModal) return;
    e.preventDefault();
    var menu = btn.closest('.scr-menu');
    if (menu) menu.hidden = true;
    current = btn.dataset.pk;

    logoModal.querySelector('[data-logo-station]').textContent = btn.dataset.station;
    var image = logoModal.querySelector('[data-logo-image]');
    var empty = logoModal.querySelector('[data-logo-empty]');
    var meta = logoModal.querySelector('[data-logo-meta]');
    var has = !!btn.dataset.logoUrl;
    image.hidden = !has;
    if (has) image.src = btn.dataset.logoUrl + '?v=' + Date.now();
    empty.hidden = has;
    meta.textContent = has
      ? (btn.dataset.logoName || 'logo.bmp') + ' · ' + kb(btn.dataset.logoSize)
      : '';
    logoRemove.hidden = !has;
    logoFile.value = '';
    logoGo.disabled = true;
    logoGo.textContent = has ? 'Replace logo' : 'Upload';
    logoWarnings.hidden = true;
    logoWarnings.innerHTML = '';
    show('settings-logo', true);
  });

  if (logoFile) logoFile.addEventListener('change', function () {
    logoGo.disabled = !logoFile.files.length;
  });

  if (logoGo) logoGo.addEventListener('click', function () {
    if (logoGo.disabled || !current || !logoFile.files.length) return;
    var form = new FormData();
    form.append('logo', logoFile.files[0]);
    logoGo.disabled = true;
    crud().postFile(urlFor('urlLogoSave', current), form).then(function (res) {
      var body = res.body || {};
      if (!body.ok) {
        logoGo.disabled = false;
        return fail(body.errors || [{ field: '', message: 'Something went wrong.' }]);
      }
      // Stored either way; the warnings say what may not print well, and the
      // page reloads once they have been read.
      if ((body.warnings || []).length) {
        logoWarnings.hidden = false;
        logoWarnings.innerHTML = '';
        body.warnings.forEach(function (text) {
          var line = document.createElement('p');
          line.className = 'scr-set-warning';
          line.textContent = text;
          logoWarnings.appendChild(line);
        });
        var next = document.createElement('button');
        next.type = 'button';
        next.className = 'scr-btn-secondary';
        next.textContent = 'Saved anyway — close';
        next.addEventListener('click', function () { done(body, 'settings-logo'); });
        logoWarnings.appendChild(next);
        return;
      }
      done(body, 'settings-logo');
    }).catch(function () {
      logoGo.disabled = false;
      fail([{ field: '', message: 'Could not reach the server. Try again.' }]);
    });
  });

  if (logoRemove) logoRemove.addEventListener('click', function () {
    if (!current) return;
    send(logoRemove, urlFor('urlLogoClear', current), {}, 'settings-logo');
  });

  /* ── Copy to other stations ───────────────────────────────────────── */

  var apply = document.querySelector('[data-scr-modal="settings-apply"]');
  var applyGo = apply && apply.querySelector('[data-apply-go]');
  var applySearch = apply && apply.querySelector('[data-apply-search]');
  var applyAll = apply && apply.querySelector('[data-apply-all]');
  var applyRows = apply ? Array.prototype.slice.call(apply.querySelectorAll('[data-apply-row]')) : [];
  var applyFields = apply ? Array.prototype.slice.call(apply.querySelectorAll('[data-apply-field]')) : [];

  function picked() {
    return applyRows.filter(function (r) {
      var box = r.querySelector('input');
      return box.checked && !box.disabled;
    }).map(function (r) { return r.dataset.id; });
  }
  function fields() {
    return applyFields.filter(function (b) { return b.checked; }).map(function (b) { return b.value; });
  }
  function syncApply() {
    if (!apply) return;
    var stations = picked().length;
    var chosen = fields().length;
    apply.querySelector('[data-apply-count]').textContent = stations + ' selected';
    apply.querySelector('[data-apply-field-count]').textContent =
      chosen + ' field' + (chosen === 1 ? '' : 's');
    applyGo.disabled = !stations || !chosen;
    var shown = applyRows.filter(function (r) { return !r.hidden && !r.querySelector('input').disabled; });
    applyAll.checked = shown.length > 0 && shown.every(function (r) { return r.querySelector('input').checked; });
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-settings-apply]');
    if (!btn || !apply) return;
    e.preventDefault();
    var menu = btn.closest('.scr-menu');
    if (menu) menu.hidden = true;
    current = btn.dataset.pk;
    apply.querySelector('[data-apply-sub]').textContent = 'From ' + btn.dataset.station;
    applyFields.forEach(function (b) { b.checked = false; });
    applyRows.forEach(function (r) {
      r.hidden = false;
      var box = r.querySelector('input');
      // The station it came from is not a destination.
      box.checked = false;
      r.classList.toggle('is-source', false);
    });
    if (applySearch) applySearch.value = '';
    syncApply();
    show('settings-apply', true);
  });

  applyRows.forEach(function (r) {
    var box = r.querySelector('input');
    r.addEventListener('click', function (e) {
      if (box.disabled) return;
      if (e.target !== box) box.checked = !box.checked;
      syncApply();
    });
  });
  applyFields.forEach(function (b) { b.addEventListener('change', syncApply); });

  if (applySearch) applySearch.addEventListener('input', function () {
    var q = applySearch.value.trim().toLowerCase();
    var any = false;
    applyRows.forEach(function (r) {
      r.hidden = !!q && r.dataset.search.indexOf(q) === -1;
      if (!r.hidden) any = true;
    });
    apply.querySelector('[data-apply-empty]').hidden = any;
    syncApply();
  });

  if (applyAll) applyAll.addEventListener('change', function () {
    applyRows.forEach(function (r) {
      var box = r.querySelector('input');
      if (!r.hidden && !box.disabled) box.checked = applyAll.checked;
    });
    syncApply();
  });

  if (applyGo) applyGo.addEventListener('click', function () {
    if (applyGo.disabled || !current) return;
    send(applyGo, urlFor('urlApply', current),
      { fields: fields(), branches: picked() }, 'settings-apply');
  });

  /* ── Deactivate ───────────────────────────────────────────────────── */

  var off = document.querySelector('[data-scr-modal="settings-off"]');
  var offGo = off && off.querySelector('[data-off-go]');

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-settings-deactivate]');
    if (!btn || !off) return;
    e.preventDefault();
    var menu = btn.closest('.scr-menu');
    if (menu) menu.hidden = true;
    current = btn.dataset.pk;
    off.querySelector('[data-off-title]').textContent = 'Deactivate settings for ' + btn.dataset.station + '?';
    offGo.disabled = false;
    show('settings-off', true);
  });

  if (offGo) offGo.addEventListener('click', function () {
    if (!current) return;
    send(offGo, urlFor('urlDeactivate', current), {}, 'settings-off');
  });
})();

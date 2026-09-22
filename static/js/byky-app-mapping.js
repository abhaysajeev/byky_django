/* byky-app-mapping.js -- App Mapping's Map Release and Remove.
 *
 * Map Release: pick a build of the current app, send it to All branches or to
 * picked ones, see the server's preview (who moves, and what must be
 * confirmed), tick every warning, save. The rules and the preview are
 * server-side (apps/devices/services.py: mapping_plan, map_release,
 * unmap_release) -- the same resolution tablets get, so the preview cannot
 * disagree with them.
 */
(function () {
  'use strict';

  var root = document.querySelector('[data-app-mapping]');
  if (!root) return;

  function crud() { return window.BykyCrud || {}; }
  function data(id) {
    var node = document.getElementById(id);
    try { return node ? JSON.parse(node.textContent) : {}; } catch (e) { return {}; }
  }
  var RELEASES = data('map-releases-data');
  var STATE = data('map-state-data');

  function show(name, on) {
    var box = document.querySelector('[data-scr-modal="' + name + '"]');
    var veil = document.querySelector('[data-scr-modal-veil="' + name + '"]');
    if (box) box.hidden = !on;
    if (veil) veil.hidden = !on;
  }

  function fail(items) {
    if (crud().showMessages) crud().showMessages({ title: 'Could not save', items: items });
  }

  function send(button, url, payload, modal) {
    button.disabled = true;
    return crud().post(url, payload).then(function (res) {
      var body = res.body || {};
      if (body.ok) {
        show(modal, false);
        if (crud().toastAfterReload) crud().toastAfterReload(body.message || 'Saved.');
        window.location.reload();
        return;
      }
      button.disabled = false;
      fail(body.errors || [{ field: '', message: 'Something went wrong.' }]);
    }).catch(function () {
      button.disabled = false;
      fail([{ field: '', message: 'Could not reach the server. Try again.' }]);
    });
  }

  /* ── which app is on screen ───────────────────────────────────────── */

  var firstPanel = root.querySelector('[data-map-app]');
  var currentApp = firstPanel ? firstPanel.dataset.mapApp : '';
  root.querySelectorAll('[data-map-app-tab]').forEach(function (tab) {
    tab.addEventListener('click', function () { currentApp = tab.dataset.mapAppTab; });
  });
  function appLabel(app) {
    var panel = root.querySelector('[data-map-app="' + app + '"]');
    return panel ? panel.dataset.mapAppLabel : app;
  }

  /* ── grid: row checkboxes and Map selected ────────────────────────── */

  function visible(tr) { return !tr.hidden && tr.offsetParent !== null; }

  root.querySelectorAll('[data-map-app]').forEach(function (panel) {
    var mapSelected = panel.querySelector('[data-map-selected]');
    var all = panel.querySelector('[data-map-check-all]');
    function boxes() { return Array.prototype.slice.call(panel.querySelectorAll('[data-map-check]')); }
    function sync() {
      var picked = boxes().filter(function (b) { return b.checked; }).length;
      if (mapSelected) {
        mapSelected.disabled = !picked;
        mapSelected.textContent = picked ? 'Map selected (' + picked + ')' : 'Map selected';
      }
    }
    boxes().forEach(function (b) { b.addEventListener('change', sync); });
    if (all) all.addEventListener('change', function () {
      boxes().forEach(function (b) { if (visible(b.closest('tr'))) b.checked = all.checked; });
      sync();
    });
    if (mapSelected) mapSelected.addEventListener('click', function () {
      var picked = boxes().filter(function (b) { return b.checked; });
      var rowsPicked = picked.map(function (b) { return b.closest('tr'); });
      var firms = rowsPicked.map(function (tr) { return tr.dataset.companyId; })
        .filter(function (v, i, all) { return all.indexOf(v) === i; });
      if (firms.length > 1) {
        // A release belongs to one company, so a selection spanning two has no
        // single build to offer.
        if (crud().showMessages) {
          crud().showMessages({
            title: 'Pick one company',
            items: [{ field: '', message: 'These stations belong to different companies. Map one company at a time.' }]
          });
        }
        return;
      }
      openMap(panel.dataset.mapApp, 'branch', picked.map(function (b) { return b.value; }),
        firms[0], rowsPicked[0] && rowsPicked[0].dataset.companyName);
    });
  });

  /* ── Map Release modal ────────────────────────────────────────────── */

  var modal = document.querySelector('[data-scr-modal="release-map"]');
  if (!modal) return;
  var sub = modal.querySelector('[data-map-sub]');
  var releaseSel = modal.querySelector('[data-map-release]');
  var releaseEmpty = modal.querySelector('[data-map-release-empty]');
  var scopes = modal.querySelectorAll('[data-map-scope]');
  var picker = modal.querySelector('[data-map-picker]');
  var search = modal.querySelector('[data-map-search]');
  var pickAll = modal.querySelector('[data-map-pick-all]');
  var count = modal.querySelector('[data-map-count]');
  var empty = modal.querySelector('[data-map-empty]');
  var rows = Array.prototype.slice.call(modal.querySelectorAll('[data-map-pick-row]'));
  var preview = modal.querySelector('[data-map-preview]');
  var summary = modal.querySelector('[data-map-summary]');
  var warningsBox = modal.querySelector('[data-map-warnings]');
  var go = modal.querySelector('[data-map-go]');
  var pickCompany = modal.querySelector('[data-map-pick-company]');
  var multiCompany = root.dataset.multiCompany === '1';
  var app = '';
  var companyId = '';
  var seq = 0;
  var planReady = false;

  function scope() {
    var on = modal.querySelector('[data-map-scope]:checked');
    return on ? on.value : '';
  }
  function picked() {
    return rows.filter(function (r) { return r.querySelector('input').checked; })
      .map(function (r) { return r.dataset.id; });
  }

  function openMap(forApp, forScope, branchIds, forCompany, companyName) {
    app = forApp || currentApp;
    companyId = String(forCompany || '');
    if (!companyId) {
      // The page's own "Map Release" button names no company. With one company
      // that is unambiguous; with several the admin picks from a row or from a
      // company's own default card.
      var known = Object.keys(RELEASES[app] || {});
      companyId = known.length === 1 ? known[0] : '';
    }
    sub.textContent = companyName ? appLabel(app) + ' · ' + companyName : appLabel(app);
    // A release belongs to one company, and so does the All-branches row, so
    // the picker never mixes them.
    var choices = (RELEASES[app] || {})[companyId] || [];
    pickCompany.hidden = !!companyId || !multiCompany;
    releaseSel.innerHTML = '<option value="">Select a release</option>';
    choices.forEach(function (c) {
      var opt = document.createElement('option');
      opt.value = c.id;
      opt.textContent = c.label;
      releaseSel.appendChild(opt);
    });
    releaseSel.disabled = !choices.length;
    releaseEmpty.hidden = !!choices.length;

    var now = STATE[app] || {};
    var wanted = (branchIds || []).map(String);
    rows.forEach(function (r) {
      var mine = !companyId || r.dataset.company === companyId;
      r.querySelector('[data-map-now]').textContent = now[r.dataset.id] || '—';
      r.querySelector('input').checked = mine && wanted.indexOf(r.dataset.id) > -1;
      r.hidden = !mine;
      r.dataset.other = mine ? '' : 'yes';
    });
    search.value = '';
    empty.hidden = true;
    scopes.forEach(function (s) { s.checked = s.value === forScope; });
    picker.hidden = forScope !== 'branch';
    syncPicker();
    refresh();
    show('release-map', true);
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-map-open]');
    if (!btn || btn.disabled) return;
    e.preventDefault();
    var menu = btn.closest('.scr-menu');
    if (menu) menu.hidden = true;
    var panel = btn.closest('[data-map-app]');
    openMap(btn.dataset.app || (panel && panel.dataset.mapApp) || currentApp,
      btn.dataset.scope || 'branch', btn.dataset.branch ? [btn.dataset.branch] : [],
      btn.dataset.company, btn.dataset.companyName);
  });

  function syncPicker() {
    var n = picked().length;
    count.textContent = n + ' selected';
    var shown = rows.filter(function (r) { return !r.hidden; });
    pickAll.checked = shown.length > 0 && shown.every(function (r) { return r.querySelector('input').checked; });
  }

  search.addEventListener('input', function () {
    var q = search.value.trim().toLowerCase();
    var any = false;
    rows.forEach(function (r) {
      r.hidden = r.dataset.other === 'yes' || (!!q && r.dataset.search.indexOf(q) === -1);
      if (!r.hidden) any = true;
    });
    empty.hidden = any;
    syncPicker();
  });
  pickAll.addEventListener('change', function () {
    rows.forEach(function (r) { if (!r.hidden) r.querySelector('input').checked = pickAll.checked; });
    syncPicker();
    refresh();
  });
  rows.forEach(function (r) {
    var box = r.querySelector('input');
    r.addEventListener('click', function (e) {
      if (e.target !== box) box.checked = !box.checked;
      syncPicker();
      refresh();
    });
  });
  scopes.forEach(function (s) {
    s.addEventListener('change', function () {
      picker.hidden = scope() !== 'branch';
      refresh();
    });
  });
  releaseSel.addEventListener('change', refresh);

  /* ── preview: asked of the server on every change ─────────────────── */

  var timer = null;
  function refresh() {
    planReady = false;
    go.disabled = true;
    clearTimeout(timer);
    var ready = releaseSel.value && (scope() === 'all' || (scope() === 'branch' && picked().length));
    if (!ready) {
      preview.hidden = true;
      return;
    }
    timer = setTimeout(askPreview, 180);
  }

  function askPreview() {
    var mine = ++seq;
    preview.hidden = false;
    summary.textContent = 'Working out what changes…';
    warningsBox.innerHTML = '';
    crud().post(root.dataset.urlPreview, payload(false)).then(function (res) {
      if (mine !== seq) return;                    // a newer change is on its way
      var body = res.body || {};
      if (!body.ok) {
        summary.textContent = ((body.errors || [])[0] || {}).message || 'Could not work out the change.';
        return;
      }
      render(body.plan);
    }).catch(function () {
      if (mine === seq) summary.textContent = 'Could not reach the server. Try again.';
    });
  }

  function render(plan) {
    summary.textContent = plan.summary + '.';
    warningsBox.innerHTML = '';
    (plan.warnings || []).forEach(function (w) {
      var label = document.createElement('label');
      label.className = 'scr-rel-warning is-' + w.code;
      var box = document.createElement('input');
      box.type = 'checkbox';
      box.className = 'scr-check';
      box.setAttribute('data-map-ack', '');
      var text = document.createElement('span');
      text.textContent = w.message;
      label.appendChild(box);
      label.appendChild(text);
      warningsBox.appendChild(label);
      box.addEventListener('change', syncGo);
    });
    planReady = plan.moving > 0 || scope() === 'all';
    syncGo();
  }

  function syncGo() {
    var acks = warningsBox.querySelectorAll('[data-map-ack]');
    var allTicked = Array.prototype.every.call(acks, function (b) { return b.checked; });
    go.disabled = !planReady || !allTicked;
  }

  function payload(confirmed) {
    return {
      release: releaseSel.value || null,
      scope: scope(),
      branches: scope() === 'branch' ? picked() : [],
      confirmed: confirmed
    };
  }

  go.addEventListener('click', function () {
    if (go.disabled || !companyId) return;
    var confirmed = warningsBox.querySelectorAll('[data-map-ack]').length > 0;
    send(go, root.dataset.urlSave, payload(confirmed), 'release-map');
  });

  /* ── Remove ───────────────────────────────────────────────────────── */

  var unmap = document.querySelector('[data-scr-modal="release-unmap"]');
  var unmapGo = unmap && unmap.querySelector('[data-unmap-go]');
  var removing = null;

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-map-remove]');
    if (!btn || btn.disabled || !unmap) return;
    e.preventDefault();
    var menu = btn.closest('.scr-menu');
    if (menu) menu.hidden = true;
    removing = btn.dataset.pk;
    unmap.querySelector('[data-unmap-title]').textContent = btn.dataset.title;
    unmap.querySelector('[data-unmap-note]').textContent = btn.dataset.note;
    unmapGo.disabled = false;
    show('release-unmap', true);
  });

  if (unmapGo) unmapGo.addEventListener('click', function () {
    if (!removing || unmapGo.disabled) return;
    send(unmapGo, root.dataset.urlRemove.replace(/\/0\/remove\/$/, '/' + removing + '/remove/'), {}, 'release-unmap');
  });
})();

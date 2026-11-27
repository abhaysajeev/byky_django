/* Suspense Entries (apps/rental/suspense.py; owner, 26 Nov 2026).
 *
 * List page: the List / Grid toggle (remembered per browser), rows and cards
 * open their entry. Both pages: the Add / Edit drawer's Suspected orders
 * picker reloads that branch's orders for the chosen date whenever either
 * changes. Detail page: Mark resolved and Cancel entry post from their dialogs.
 * The server has the last word on every rule.
 */
(function () {
  'use strict';

  var root = document.querySelector('[data-suspense]');
  if (!root) return;
  var Crud = window.BykyCrud || {};
  var VIEW_KEY = 'byky.suspense.view';

  /* ── list / grid ─────────────────────────────────────────────────── */

  function storedView() {
    try { return window.localStorage.getItem(VIEW_KEY) || 'list'; } catch (err) { return 'list'; }
  }
  function showView(view) {
    root.querySelectorAll('[data-sus-pane]').forEach(function (pane) { pane.hidden = pane.dataset.susPane !== view; });
    root.querySelectorAll('[data-sus-view]').forEach(function (btn) {
      var on = btn.dataset.susView === view;
      btn.classList.toggle('is-on', on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }
  if (root.querySelector('[data-sus-view]')) {
    showView(storedView());
    root.querySelectorAll('[data-sus-view]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        showView(btn.dataset.susView);
        try { window.localStorage.setItem(VIEW_KEY, btn.dataset.susView); } catch (err) { /* private mode */ }
      });
    });
  }

  /* A row or card opens its entry, unless the click was on a link or button in it. */
  root.addEventListener('click', function (e) {
    var target = e.target.closest('[data-href]');
    if (!target || e.target.closest('a, button')) return;
    window.location.href = target.dataset.href;
  });

  /* ── the drawer's Suspected orders picker ──────────────────────────── */

  function setupDrawer() {
    var drawer = document.getElementById('drawerSuspense');
    if (!drawer || !drawer.__bykyFill || drawer.__susWrapped) return;
    drawer.__susWrapped = true;
    var field = function (name) { return drawer.querySelector('[data-field="' + name + '"]'); };
    var multi = field('suspected_orders');
    var branch = field('branch');
    var date = field('entry_date');
    var request = 0;

    function reload(chosen) {
      if (!multi || !window.BykyMulti) return;
      var keep = chosen || window.BykyMulti.values(multi);
      if (!branch.value || !date.value) {
        multi.dataset.emptyText = 'Pick a branch and a date to list its orders.';
        window.BykyMulti.setOptions(multi, [], []);
        return;
      }
      var mine = ++request;
      var url = root.dataset.ordersUrl + '?branch=' + encodeURIComponent(branch.value) +
        '&date=' + encodeURIComponent(date.value);
      fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
        .then(function (r) { return r.json(); })
        .then(function (body) {
          if (mine !== request) return;          // a newer branch/date won
          multi.dataset.emptyText = 'No orders at this branch on that day.';
          window.BykyMulti.setOptions(multi, (body && body.orders) || [], keep);
        })
        .catch(function () {
          if (mine !== request) return;
          multi.dataset.emptyText = 'Could not load the orders. Check the connection and try again.';
          window.BykyMulti.setOptions(multi, [], keep);
        });
    }

    var fill = drawer.__bykyFill;
    drawer.__bykyFill = function (mode, record) {
      fill(mode, record);
      if (mode !== 'edit') {
        if (date && !date.value && root.dataset.today) {
          if (date._flatpickr) date._flatpickr.setDate(root.dataset.today, false, 'd M Y');
          else date.value = root.dataset.today;
        }
        if (branch && !branch.value && branch.options.length === 2) {
          branch.value = branch.options[1].value;
          branch.classList.add('has-value');
        }
      }
      reload(mode === 'edit' && record ? (record.suspected_orders || []).map(String) : []);
    };
    [branch, date].forEach(function (el) {
      if (el) el.addEventListener('change', function () { reload(); });
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', setupDrawer);
  else setupDrawer();

  /* ── detail: Mark resolved / Cancel entry ──────────────────────────── */

  function busy(go, modal, on) {
    go.disabled = on;
    go.classList.toggle('is-saving', on);
    modal.querySelectorAll('[data-scr-modal-close]').forEach(function (close) { close.disabled = on; });
  }

  root.querySelectorAll('[data-sus-go]').forEach(function (go) {
    go.addEventListener('click', function () {
      if (go.disabled || !Crud.post) return;
      var modal = go.closest('.scr-modal');
      var action = go.dataset.susGo;
      var note = (modal.querySelector('[data-sus-note]') || {}).value || '';
      if (action === 'cancel' && !note.trim()) {
        Crud.showMessages({ title: 'Please fix the following',
          items: [{ field: 'Reason', message: 'Say why the entry is cancelled.' }] });
        return;
      }
      var url = action === 'resolve' ? root.dataset.resolveUrl : root.dataset.cancelUrl;
      var payload = action === 'resolve' ? { remarks: note } : { reason: note };
      busy(go, modal, true);
      Crud.post(url, payload).then(function (result) {
        var body = result.body || {};
        if (body.ok) {
          if (Crud.toastAfterReload) Crud.toastAfterReload(body.message);
          window.location.href = body.redirect || window.location.href;
          return;
        }
        busy(go, modal, false);
        Crud.showMessages({ title: result.status === 403 ? 'Not allowed' : 'Could not save',
          items: body.errors || [{ field: '', message: 'Something went wrong.' }] });
      });
    });
  });
})();

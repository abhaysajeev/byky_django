/* byky-app-release.js -- App Releases' Withdraw / Restore, and the confirm
 * before an edit changes the update type of a build branches are already on.
 *
 * Add and Edit go through the shared drawer (byky-crud.js posts it). The
 * rules are server-side (apps/devices/services.py: create_release,
 * update_release, withdraw_release, restore_release); this only asks first.
 */
(function () {
  'use strict';

  var root = document.querySelector('[data-app-release]');
  if (!root) return;

  function crud() { return window.BykyCrud || {}; }

  var modal = document.querySelector('[data-scr-modal="release-state"]');
  var go = modal && modal.querySelector('[data-release-go]');

  function show(on) {
    var veil = document.querySelector('[data-scr-modal-veil="release-state"]');
    if (modal) modal.hidden = !on;
    if (veil) veil.hidden = !on;
  }

  function fail(items) {
    if (crud().showMessages) crud().showMessages({ title: 'Could not save', items: items });
  }

  /* One modal, three uses: Withdraw, Restore, and the update-type confirm.
     `onGo` is what its main button does this time. */
  var onGo = null;

  function ask(opts) {
    if (!modal) return;
    modal.querySelector('[data-release-title]').textContent = opts.title;
    modal.querySelector('[data-release-sub]').textContent = opts.sub || '';
    modal.querySelector('[data-release-note]').textContent = opts.note || '';
    go.textContent = opts.button;
    go.className = opts.danger ? 'scr-btn-danger' : 'scr-btn-primary';
    go.disabled = false;
    onGo = opts.onGo;
    show(true);
  }

  if (go) go.addEventListener('click', function () {
    if (go.disabled || !onGo) return;
    onGo();
  });

  function post(url) {
    go.disabled = true;
    crud().post(url, {}).then(function (res) {
      var body = res.body || {};
      if (body.ok) {
        show(false);
        if (crud().toastAfterReload) crud().toastAfterReload(body.message || 'Saved.');
        window.location.reload();
        return;
      }
      go.disabled = false;
      fail(body.errors || [{ field: '', message: 'Something went wrong.' }]);
    }).catch(function () {
      go.disabled = false;
      fail([{ field: '', message: 'Could not reach the server. Try again.' }]);
    });
  }

  function urlFor(kind, pk) {
    return root.dataset[kind === 'withdraw' ? 'urlWithdraw' : 'urlRestore']
      .replace(/\/0\/([a-z]+)\/$/, '/' + pk + '/$1/');
  }

  /* ── Withdraw / Restore from the row menu ─────────────────────────── */

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-release-action]');
    if (!btn || btn.disabled) return;
    e.preventDefault();
    var kind = btn.dataset.releaseAction;
    var withdraw = kind === 'withdraw';
    ask({
      title: (withdraw ? 'Withdraw ' : 'Restore ') + btn.dataset.label + '?',
      sub: withdraw ? 'Tablets stop being offered this build.' : 'It can be offered to tablets again.',
      note: btn.dataset.note,
      button: withdraw ? 'Withdraw' : 'Restore',
      danger: withdraw,
      onGo: function () { post(urlFor(kind, btn.dataset.pk)); }
    });
  });

  /* ── Edit: confirm a new update type on a build branches are on ───── */

  var editing = null;
  var confirmed = false;

  document.addEventListener('click', function (e) {
    var opener = e.target.closest('[data-scr-open^="app_release:"]');
    if (!opener) return;
    editing = null;
    if (opener.dataset.scrOpen === 'app_release:edit') {
      var row = opener.closest('.scr-row');
      var node = row && document.getElementById(row.dataset.recordId);
      editing = node ? JSON.parse(node.textContent) : null;
    }
  });

  /* Capture phase: runs before byky-crud.js's save handler, so the save can
     be held until the admin has read what the change does. */
  document.addEventListener('click', function (e) {
    var save = e.target.closest('#drawerAppRelease [data-scr-save]');
    if (!save || !editing || confirmed) { confirmed = false; return; }
    var picked = document.querySelector('#drawerAppRelease [data-field="update_type"]');
    if (!picked || picked.value === editing.update_type || !editing.mapped_count) return;

    e.preventDefault();
    e.stopImmediatePropagation();
    var mandatory = picked.value === 'mandatory';
    var where = editing.is_all
      ? 'It is the All-branches release' + (editing.branch_count ? ' and mapped to ' + editing.branch_count + ' more branches' : '')
      : 'It is mapped to ' + editing.branch_count + ' branch' + (editing.branch_count === 1 ? '' : 'es');
    ask({
      title: 'Make ' + editing.title + (mandatory ? ' Mandatory?' : ' Any time?'),
      sub: where + '.',
      note: mandatory
        ? 'Tablets behind it will be stopped at their next start and cannot log in until they install it.'
        : 'Tablets will be offered it with Update and Later, and only while their branch is open.',
      button: mandatory ? 'Make Mandatory' : 'Make Any time',
      danger: mandatory,
      onGo: function () {
        show(false);
        confirmed = true;
        save.click();
      }
    });
  }, true);
})();

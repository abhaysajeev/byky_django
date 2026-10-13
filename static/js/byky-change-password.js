/* byky-change-password.js -- the header's Change Password dialog, on every
 * screen (navbar.html). Self-contained: the dashboard does not load
 * byky-screen.js, so open / close / Escape are handled here. Posts through
 * BykyCrud.post (byky-crud.js, loaded on every page) to account-password;
 * the rules are server-side (portal.auth.change_own_password), and each
 * refusal is shown under the field it is about.
 */
(function () {
  'use strict';

  var modal = document.querySelector('[data-cp-modal]');
  var veil = document.querySelector('[data-cp-veil]');
  if (!modal) return;
  var save = modal.querySelector('[data-cp-save]');

  function fields() { return modal.querySelectorAll('[data-cp-field]'); }

  function clearErrors() {
    modal.querySelectorAll('[data-cp-error]').forEach(function (p) { p.textContent = ''; p.hidden = true; });
  }

  function showError(field, message) {
    var p = modal.querySelector('[data-cp-error="' + (field || '') + '"]') ||
            modal.querySelector('[data-cp-error=""]');
    p.textContent = message;
    p.hidden = false;
  }

  function open() {
    fields().forEach(function (input) { input.value = ''; });
    clearErrors();
    save.disabled = false;
    veil.hidden = false;
    modal.hidden = false;
    modal.querySelector('[data-cp-field="current"]').focus();
  }

  function close() {
    veil.hidden = true;
    modal.hidden = true;
  }

  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-cp-open]')) { e.preventDefault(); open(); }
  });
  modal.querySelectorAll('[data-cp-close]').forEach(function (b) { b.addEventListener('click', close); });
  veil.addEventListener('click', close);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !modal.hidden) close();
    if (e.key === 'Enter' && !modal.hidden && e.target.closest('[data-cp-field]')) save.click();
  });

  save.addEventListener('click', function () {
    var crud = window.BykyCrud || {};
    if (save.disabled || !crud.post) return;
    clearErrors();
    var payload = {};
    fields().forEach(function (input) { payload[input.dataset.cpField] = input.value; });
    save.disabled = true;
    crud.post(modal.dataset.url, payload).then(function (res) {
      var body = res.body || {};
      if (body.ok) {
        close();
        if (crud.toast) crud.toast(body.message || 'Password changed.');
        return;
      }
      save.disabled = false;
      (body.errors || [{ field: '', message: 'Something went wrong. Try again.' }]).forEach(function (err) {
        showError(err.field, err.message);
      });
    }).catch(function () {
      save.disabled = false;
      showError('', 'Could not reach the server. Try again.');
    });
  });
})();

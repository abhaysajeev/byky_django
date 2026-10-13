/* byky-sync-issue.js -- Sync Issues detail: Replay and Dismiss.
 *
 * Replay posts the request as edited in the textarea; the server checks it is
 * a JSON object with the same sync_id and runs it as the original session
 * (apps/sync/services.py: replay, dismiss). Either way the page reloads to
 * show the new attempt and status.
 */
(function () {
  'use strict';

  var root = document.querySelector('[data-sync-issue]');
  if (!root) return;

  function crud() { return window.BykyCrud || {}; }

  function show(name, on) {
    var box = document.querySelector('[data-scr-modal="' + name + '"]');
    var veil = document.querySelector('[data-scr-modal-veil="' + name + '"]');
    if (box) box.hidden = !on;
    if (veil) veil.hidden = !on;
  }

  function send(button, url, payload, title) {
    if (button.disabled || !crud().post) return;
    button.disabled = true;
    crud().post(url, payload).then(function (res) {
      var body = res.body || {};
      if (body.ok) {
        if (crud().toastAfterReload) crud().toastAfterReload(body.message || 'Done.');
        window.location.reload();
        return;
      }
      button.disabled = false;
      if (crud().showMessages) {
        crud().showMessages({ title: title, items: body.errors || [{ field: '', message: 'Something went wrong.' }] });
      }
    }).catch(function () {
      button.disabled = false;
      if (crud().showMessages) {
        crud().showMessages({ title: title, items: [{ field: '', message: 'Could not reach the server. Try again.' }] });
      }
    });
  }

  /* ── Replay ───────────────────────────────────────────────────────── */

  var replay = root.querySelector('[data-sync-replay]');
  var body = root.querySelector('[data-sync-body]');
  var bodyError = root.querySelector('[data-sync-body-error]');

  if (replay && body) replay.addEventListener('click', function () {
    var parsed;
    try {
      parsed = JSON.parse(body.value);
    } catch (e) {
      bodyError.textContent = 'Not valid JSON: ' + e.message;
      bodyError.hidden = false;
      body.focus();
      return;
    }
    bodyError.hidden = true;
    send(replay, root.dataset.urlReplay, { request_data: parsed }, 'Could not replay');
  });

  /* ── Dismiss ──────────────────────────────────────────────────────── */

  var dismiss = root.querySelector('[data-sync-dismiss]');
  var dismissGo = document.querySelector('[data-sync-dismiss-go]');
  var reason = document.querySelector('[data-sync-dismiss-reason]');

  if (dismiss) dismiss.addEventListener('click', function () {
    if (reason) reason.value = '';
    show('sync-dismiss', true);
    if (reason) reason.focus();
  });

  if (dismissGo) dismissGo.addEventListener('click', function () {
    send(dismissGo, root.dataset.urlDismiss, { reason: reason ? reason.value : '' }, 'Could not dismiss');
  });
})();

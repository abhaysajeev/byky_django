/* byky-order-cancel.js -- Cancel order, on a running order's page.
 *
 * The modal is opened by byky-screen.js (data-scr-modal-open="order-cancel").
 * This posts the reason; the rules are server-side
 * (apps/rental/requests.py::cancel_directly). On success the page reloads to
 * show the order cancelled.
 */
(function () {
  'use strict';

  var root = document.querySelector('[data-order-cancel]');
  if (!root) return;
  var go = document.querySelector('[data-cancel-go]');
  var reason = document.querySelector('[data-cancel-reason]');
  if (!go || !reason) return;

  function crud() { return window.BykyCrud || {}; }

  function fail(items) {
    go.disabled = false;
    if (crud().showMessages) crud().showMessages({ title: 'Could not cancel', items: items });
  }

  go.addEventListener('click', function () {
    if (go.disabled || !crud().post) return;
    if (!reason.value.trim()) {
      fail([{ field: 'Reason', message: 'Give the reason for the cancel.' }]);
      reason.focus();
      return;
    }
    go.disabled = true;
    crud().post(root.dataset.url, { reason: reason.value }).then(function (res) {
      var body = res.body || {};
      if (body.ok) {
        if (crud().toastAfterReload) crud().toastAfterReload(body.message || 'Order cancelled.');
        window.location.reload();
        return;
      }
      fail(body.errors || [{ field: '', message: 'Something went wrong.' }]);
    }).catch(function () {
      fail([{ field: '', message: 'Could not reach the server. Try again.' }]);
    });
  });
})();

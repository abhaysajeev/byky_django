/*
 * Blocked Customers (apps/rental/blocked.py): Block a customer, and Unblock
 * one from the Blocked now tab -- both with a reason, one modal for both.
 * Posts {customer, blocked, reason} to data-change-url, then reloads.
 */
(function () {
  'use strict';

  var root = document.querySelector('[data-blocked-customers]');
  var modal = document.querySelector('[data-scr-modal="customer-block"]');
  if (!root || !modal) return;

  function crud() { return window.BykyCrud || {}; }

  var title = modal.querySelector('[data-block-title]');
  var sub = modal.querySelector('[data-block-sub]');
  var pick = modal.querySelector('[data-block-pick]');
  var picker = modal.querySelector('[data-block-customer]');
  var reason = modal.querySelector('[data-block-reason]');
  var go = modal.querySelector('[data-block-go]');
  var current = null;     // {customer, blocked}

  function show(on) {
    modal.hidden = !on;
    var veil = document.querySelector('[data-scr-modal-veil="customer-block"]');
    if (veil) veil.hidden = !on;
  }

  function chosenId() {
    var text = picker.value.trim();
    var option = Array.prototype.find.call(document.querySelectorAll('#block-customers option'), function (o) {
      return o.value === text;
    });
    return option ? option.dataset.id : null;
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-blocked-open]');
    if (!btn) return;
    e.preventDefault();
    var unblocking = btn.dataset.blockedOpen === 'unblock';
    current = { customer: unblocking ? btn.dataset.customer : null, blocked: !unblocking };
    title.textContent = unblocking ? 'Unblock ' + btn.dataset.name + '?' : 'Block a customer';
    sub.textContent = unblocking ? 'They can rent again. The reason is kept in the history.'
      : 'They cannot rent until unblocked. The reason is kept in the history.';
    pick.hidden = unblocking;
    picker.value = '';
    reason.value = '';
    go.textContent = unblocking ? 'Unblock' : 'Block';
    go.className = unblocking ? 'scr-btn-primary' : 'scr-btn-danger';
    go.disabled = false;
    show(true);
    (unblocking ? reason : picker).focus();
  });

  go.addEventListener('click', function () {
    if (!current || go.disabled || !crud().post) return;
    var customer = current.blocked ? chosenId() : current.customer;
    if (!customer) {
      if (crud().showMessages) crud().showMessages({ title: 'Choose a customer', items: [{ field: 'Customer', message: 'Pick a customer from the list.' }] });
      return;
    }
    go.disabled = true;
    crud().post(root.dataset.changeUrl, { customer: customer, blocked: current.blocked, reason: reason.value })
      .then(function (res) {
        var body = res.body || {};
        if (body.ok) {
          show(false);
          if (crud().toastAfterReload) crud().toastAfterReload(body.message || 'Saved.');
          window.location.reload();
          return;
        }
        go.disabled = false;
        if (crud().showMessages) crud().showMessages({ title: 'Could not save', items: body.errors || [{ field: '', message: 'Something went wrong.' }] });
      })
      .catch(function () {
        go.disabled = false;
        if (crud().showMessages) crud().showMessages({ title: 'Could not save', items: [{ field: '', message: 'Could not reach the server. Try again.' }] });
      });
  });
})();

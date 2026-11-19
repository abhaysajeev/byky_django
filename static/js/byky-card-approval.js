/* Card Discount Approval detail (apps/discount/templates/discount/approval_detail.html).
   Approve / Reject / Revoke open one dialog (byky-screen.js opens and closes
   it); this sets which decision it is and posts it with the remarks -- for a
   revoke, the reason, which is optional (owner, 3 Nov 2026). */
(function () {
  'use strict';

  var root = document.querySelector('[data-card-approval]');
  var modal = root && root.querySelector('[data-scr-modal="claim-decision"]');
  if (!modal) return;

  var title = modal.querySelector('[data-decision-title]');
  var go = modal.querySelector('[data-decision-go]');
  var goText = go.querySelector('[data-decision-text]') || go;
  var remarks = modal.querySelector('[data-decision-remarks]');
  var label = modal.querySelector('[data-decision-label]');
  var decision = 'approve';
  var TITLES = { approve: 'Approve request', reject: 'Reject request', revoke: 'Revoke approval' };
  var BUTTONS = { approve: 'Approve', reject: 'Reject', revoke: 'Revoke' };

  root.querySelectorAll('[data-decision]').forEach(function (button) {
    button.addEventListener('click', function () {
      decision = button.dataset.decision;
      title.textContent = TITLES[decision];
      goText.textContent = BUTTONS[decision];
      go.className = decision === 'approve' ? 'scr-btn-primary' : 'scr-btn-danger';
      if (label) label.textContent = decision === 'revoke' ? 'Reason (optional)' : 'Remarks';
      remarks.value = '';
    });
  });

  /* While a decision is being sent: the button spins and the dialog cannot be
     closed or sent twice (owner, 19 Nov 2026). On success it stays busy until
     the next page loads; on a refusal it is released. */
  function busy(on) {
    go.disabled = on;
    go.classList.toggle('is-saving', on);
    if (on) go.setAttribute('aria-busy', 'true'); else go.removeAttribute('aria-busy');
    modal.querySelectorAll('[data-scr-modal-close]').forEach(function (close) { close.disabled = on; });
  }

  go.addEventListener('click', function () {
    if (go.disabled) return;
    var text = remarks.value.trim();
    busy(true);
    var url = { approve: root.dataset.approveUrl, reject: root.dataset.rejectUrl,
                revoke: root.dataset.revokeUrl }[decision];
    var payload = decision === 'revoke' ? { note: text } : { remarks: text };
    window.BykyCrud.post(url, payload).then(function (result) {
      var body = result.body || {};
      if (body.ok) {
        window.BykyCrud.toastAfterReload(body.message);
        // Approve and Reject go back to the Pending list; Revoke stays here.
        if (body.redirect) window.location.href = body.redirect; else window.location.reload();
        return;
      }
      busy(false);
      window.BykyCrud.showMessages({ title: 'Could not complete',
        items: body.errors || [{ field: '', message: 'Something went wrong.' }] });
    }).catch(function () {                         // no connection: release and say so
      busy(false);
      window.BykyCrud.showMessages({ title: 'Could not complete',
        items: [{ field: '', message: 'The server could not be reached. Check the connection and try again.' }] });
    });
  });
})();

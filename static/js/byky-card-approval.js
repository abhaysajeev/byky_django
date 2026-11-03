/* Card Discount Approval detail (apps/discount/templates/discount/approval_detail.html).
   Approve / Reject / Revoke open one dialog (byky-screen.js opens and closes
   it); this sets which decision it is and posts it with the remarks -- for a
   revoke, the reason, which is required. */
(function () {
  'use strict';

  var root = document.querySelector('[data-card-approval]');
  var modal = root && root.querySelector('[data-scr-modal="claim-decision"]');
  if (!modal) return;

  var title = modal.querySelector('[data-decision-title]');
  var go = modal.querySelector('[data-decision-go]');
  var remarks = modal.querySelector('[data-decision-remarks]');
  var label = modal.querySelector('[data-decision-label]');
  var decision = 'approve';
  var TITLES = { approve: 'Approve request', reject: 'Reject request', revoke: 'Revoke approval' };
  var BUTTONS = { approve: 'Approve', reject: 'Reject', revoke: 'Revoke' };

  root.querySelectorAll('[data-decision]').forEach(function (button) {
    button.addEventListener('click', function () {
      decision = button.dataset.decision;
      title.textContent = TITLES[decision];
      go.textContent = BUTTONS[decision];
      go.className = decision === 'approve' ? 'scr-btn-primary' : 'scr-btn-danger';
      if (label) label.textContent = decision === 'revoke' ? 'Reason (required)' : 'Remarks';
      remarks.value = '';
    });
  });

  go.addEventListener('click', function () {
    if (go.disabled) return;
    var text = remarks.value.trim();
    if (decision === 'revoke' && !text) {
      window.BykyCrud.showMessages({ title: 'Could not complete',
        items: [{ field: 'Reason', message: 'Give a reason for revoking.' }] });
      return;
    }
    go.disabled = true;
    var url = { approve: root.dataset.approveUrl, reject: root.dataset.rejectUrl,
                revoke: root.dataset.revokeUrl }[decision];
    var payload = decision === 'revoke' ? { note: text } : { remarks: text };
    window.BykyCrud.post(url, payload).then(function (result) {
      var body = result.body || {};
      if (body.ok) {
        window.BykyCrud.toastAfterReload(body.message);
        window.location.reload();
        return;
      }
      go.disabled = false;
      window.BykyCrud.showMessages({ title: 'Could not complete',
        items: body.errors || [{ field: '', message: 'Something went wrong.' }] });
    });
  });
})();

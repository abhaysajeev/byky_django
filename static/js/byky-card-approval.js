/* Card Discount Approval detail (apps/discount/templates/discount/approval_detail.html).
   Approve / Reject open one dialog (byky-screen.js opens and closes it); this
   sets which decision it is and posts it with the remarks. */
(function () {
  'use strict';

  var root = document.querySelector('[data-card-approval]');
  var modal = root && root.querySelector('[data-scr-modal="claim-decision"]');
  if (!modal) return;

  var title = modal.querySelector('[data-decision-title]');
  var go = modal.querySelector('[data-decision-go]');
  var remarks = modal.querySelector('[data-decision-remarks]');
  var decision = 'approve';

  root.querySelectorAll('[data-decision]').forEach(function (button) {
    button.addEventListener('click', function () {
      decision = button.dataset.decision;
      var approve = decision === 'approve';
      title.textContent = approve ? 'Approve request' : 'Reject request';
      go.textContent = approve ? 'Approve' : 'Reject';
      go.className = approve ? 'scr-btn-primary' : 'scr-btn-danger';
      remarks.value = '';
    });
  });

  go.addEventListener('click', function () {
    if (go.disabled) return;
    go.disabled = true;
    var url = decision === 'approve' ? root.dataset.approveUrl : root.dataset.rejectUrl;
    window.BykyCrud.post(url, { remarks: remarks.value.trim() }).then(function (result) {
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

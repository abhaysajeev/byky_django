/* Service & Maintenance (apps/fleet/templates/fleet/service_request_detail.html).
   Approve, Reject and Back in service each have a dialog (byky-screen.js opens
   and closes them); this posts the one clicked with its note.
   apps/fleet/service_requests.py has the last word (a note is required to put
   something back in service). */
(function () {
  'use strict';

  var root = document.querySelector('[data-service-request]');
  if (!root) return;

  root.querySelectorAll('[data-sr-go]').forEach(function (go) {
    go.addEventListener('click', function () {
      if (go.disabled) return;
      var modal = go.closest('.scr-modal');
      var action = go.dataset.srGo;
      var url = { approve: root.dataset.approveUrl, reject: root.dataset.rejectUrl,
                  complete: root.dataset.completeUrl }[action];
      var note = modal.querySelector('[data-sr-note]');
      var payload = { note: note ? note.value.trim() : '' };
      if (action === 'complete' && !payload.note) {
        window.BykyCrud.showMessages({ title: 'Could not complete',
          items: [{ field: '', message: 'Say what was done.' }] });
        return;
      }

      go.disabled = true;
      window.BykyCrud.post(url, payload).then(function (result) {
        var body = result.body || {};
        if (body.ok) {
          window.BykyCrud.toastAfterReload(body.message);
          window.location.href = body.redirect || window.location.href;
          return;
        }
        go.disabled = false;
        window.BykyCrud.showMessages({ title: 'Could not complete',
          items: body.errors || [{ field: '', message: 'Something went wrong.' }] });
      });
    });
  });
})();

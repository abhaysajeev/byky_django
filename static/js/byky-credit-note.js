/* Credit notes (apps/rental/templates/rental/credit_note_detail.html and the
   Issue dialog on invoice_detail.html). Approve / Reject / Issue each have a
   dialog (byky-screen.js opens and closes them); this posts the one clicked.
   While the amount is typed it shows the VAT inside it -- the same rule as
   apps/rental/credit_notes.py::vat_split: tax = net x rate / (100 + rate). */
(function () {
  'use strict';

  var root = document.querySelector('[data-credit-note]');
  if (!root) return;

  function money(value) { return (Math.round(value * 1000) / 1000).toFixed(3); }

  root.querySelectorAll('[data-cn-amount]').forEach(function (field) {
    var input = field.querySelector('[data-cn-net]');
    var note = field.querySelector('[data-cn-split]');
    var rate = parseFloat(field.dataset.cnRate) || 0;
    var max = parseFloat(field.dataset.cnMax);
    var plain = note.textContent;
    input.addEventListener('input', function () {
      var net = parseFloat(input.value);
      note.classList.remove('is-error');
      if (!(net > 0)) { note.textContent = plain; return; }
      if (!isNaN(max) && net > max) {
        note.textContent = 'More than the order\'s net amount, AED ' + money(max) + '.';
        note.classList.add('is-error');
        return;
      }
      var tax = rate ? Math.round(net * rate / (100 + rate) * 1000) / 1000 : 0;
      note.textContent = 'VAT ' + rate + '% included: AED ' + money(tax) + ' · taxable AED ' + money(net - tax);
    });
  });

  root.querySelectorAll('[data-cn-go]').forEach(function (go) {
    go.addEventListener('click', function () {
      if (go.disabled) return;
      var modal = go.closest('.scr-modal');
      var action = go.dataset.cnGo;
      var url = { approve: root.dataset.approveUrl, reject: root.dataset.rejectUrl,
                  issue: root.dataset.issueUrl }[action];
      var net = modal.querySelector('[data-cn-net]');
      var remarks = modal.querySelector('[data-cn-remarks]');
      var reason = modal.querySelector('[data-cn-reason]');
      var payload = {};
      if (net) payload.net_amount = net.value;
      if (remarks) payload.remarks = remarks.value.trim();
      if (reason) payload.reason = reason.value.trim();

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

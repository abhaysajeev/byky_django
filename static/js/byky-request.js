/* Requests (apps/rental/templates/rental/request_detail.html). Approve,
   Reject and Revoke each have a dialog (byky-screen.js opens and closes them);
   this posts the one clicked. Approve takes a rule -- a percentage (up to 100,
   2 decimals) or an AED amount (3 decimals) -- the same checks as
   apps/rental/requests.py::parse_rule, which has the last word. */
(function () {
  'use strict';

  var root = document.querySelector('[data-order-request]');
  if (!root) return;

  var RULES = {
    percent: { label: 'Percentage', step: '0.01', max: '100', placeholder: '0.00', help: 'Up to 100%, two decimals.' },
    amount: { label: 'Amount (AED)', step: '0.001', max: '', placeholder: '0.000', help: 'AED, up to three decimals.' }
  };

  var value = root.querySelector('[data-rq-value]');
  var label = root.querySelector('[data-rq-value-label]');
  var help = root.querySelector('[data-rq-help]');

  function chosenType() {
    var on = root.querySelector('[data-rq-type]:checked');
    return on ? on.value : 'percent';
  }

  root.querySelectorAll('[data-rq-type]').forEach(function (radio) {
    radio.addEventListener('change', function () {
      var rule = RULES[chosenType()];
      label.firstChild.textContent = rule.label + ' ';
      value.step = rule.step;
      value.placeholder = rule.placeholder;
      if (rule.max) value.max = rule.max; else value.removeAttribute('max');
      help.textContent = rule.help;
      value.focus();
    });
  });

  root.querySelectorAll('[data-rq-go]').forEach(function (go) {
    go.addEventListener('click', function () {
      if (go.disabled) return;
      var modal = go.closest('.scr-modal');
      var action = go.dataset.rqGo;
      var url = { approve: root.dataset.approveUrl, reject: root.dataset.rejectUrl,
                  revoke: root.dataset.revokeUrl }[action];
      var note = modal.querySelector('[data-rq-note]');
      var payload = { note: note ? note.value.trim() : '' };
      if (action === 'approve') {
        payload.discount_type = chosenType();
        payload.discount_value = value.value.trim();
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

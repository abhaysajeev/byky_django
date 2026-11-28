/* Requests (apps/rental/templates/rental/request_detail.html). Approve,
   Reject and Revoke each have a dialog (byky-screen.js opens and closes them);
   this posts the one clicked. Approving a discount takes a rule -- a
   percentage (up to 100, 2 decimals) or an AED amount (3 decimals) -- the same
   checks as apps/rental/requests.py::parse_rule, which has the last word; a
   complimentary or a reprint is approved with a note only. */
(function () {
  'use strict';

  var root = document.querySelector('[data-order-request]');
  if (!root) return;

  var RULES = {
    percent: { label: 'Percentage', step: '0.01', max: '100', placeholder: '0.00', help: 'Up to 100%, two decimals.' },
    amount: (function () {   /* the company's decimals (CR1) */
      var p = (parseInt(document.body.dataset.moneyPlaces, 10) || 3);
      return { label: 'Amount (AED)', step: (1 / Math.pow(10, p)).toFixed(p), max: '',
               placeholder: (0).toFixed(p), help: 'AED, up to ' + (p === 2 ? 'two' : 'three') + ' decimals.' };
    })()
  };

  var value = root.querySelector('[data-rq-value]');
  var label = root.querySelector('[data-rq-value-label]');
  var help = root.querySelector('[data-rq-help]');

  function chosenType() {
    var on = root.querySelector('[data-rq-type]:checked');
    return on ? on.value : 'amount';      /* Amount is the default (client, 28 Nov 2026) */
  }

  /* The value box follows the ticked rule: its label, step, limit and help. */
  function applyRule() {
    if (!value) return;
    var rule = RULES[chosenType()];
    label.firstChild.textContent = rule.label + ' ';
    value.step = rule.step;
    value.placeholder = rule.placeholder;
    if (rule.max) value.max = rule.max; else value.removeAttribute('max');
    help.textContent = rule.help;
  }
  applyRule();          /* on load too: the company's decimals (CR1) */

  root.querySelectorAll('[data-rq-type]').forEach(function (radio) {
    radio.addEventListener('change', function () {
      applyRule();
      value.focus();
    });
  });

  /* While a decision is being sent: the button spins and the dialog cannot be
     closed or sent twice (owner, 19 Nov 2026). On success it stays busy until
     the next page loads; on a refusal it is released so the error can be read
     and the decision retried. */
  function busy(go, modal, on) {
    go.disabled = on;
    go.classList.toggle('is-saving', on);
    if (on) go.setAttribute('aria-busy', 'true'); else go.removeAttribute('aria-busy');
    modal.querySelectorAll('[data-scr-modal-close]').forEach(function (close) { close.disabled = on; });
  }

  root.querySelectorAll('[data-rq-go]').forEach(function (go) {
    go.addEventListener('click', function () {
      if (go.disabled) return;
      var modal = go.closest('.scr-modal');
      var action = go.dataset.rqGo;
      var url = { approve: root.dataset.approveUrl, reject: root.dataset.rejectUrl,
                  revoke: root.dataset.revokeUrl }[action];
      var note = modal.querySelector('[data-rq-note]');
      var payload = { note: note ? note.value.trim() : '' };
      // Only a discount carries a rule; a complimentary or a reprint is approved as it is.
      if (action === 'approve' && root.dataset.kind === 'discount') {
        payload.discount_type = chosenType();
        payload.discount_value = value.value.trim();
      }

      busy(go, modal, true);
      window.BykyCrud.post(url, payload).then(function (result) {
        var body = result.body || {};
        if (body.ok) {
          window.BykyCrud.toastAfterReload(body.message);
          window.location.href = body.redirect || window.location.href;
          return;
        }
        busy(go, modal, false);
        window.BykyCrud.showMessages({ title: 'Could not complete',
          items: body.errors || [{ field: '', message: 'Something went wrong.' }] });
      }).catch(function () {                       // no connection: release and say so
        busy(go, modal, false);
        window.BykyCrud.showMessages({ title: 'Could not complete',
          items: [{ field: '', message: 'The server could not be reached. Check the connection and try again.' }] });
      });
    });
  });
})();

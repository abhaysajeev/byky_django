/* Invoice print preview (Rental > Invoices).

   One modal per page ([data-invoice-modal], rental/partials/
   invoice_print_modal.html). Anything carrying data-invoice-receipt="<url>"
   -- a list row, the row menu's Print item, the detail page's Print button --
   loads that invoice's receipt fragment into the modal and opens it. Print
   itself is byky-screen.js's [data-scr-print] (window.print); the receipt's
   own stylesheet puts only the receipt on paper.

   The modal's open/close follows byky-screen.js's centered modals: the veil
   and the modal are shown by clearing `hidden`; its close buttons, the veil
   and Escape are already wired there. */
(function () {
  'use strict';

  var modal = document.querySelector('[data-invoice-modal]');
  if (!modal) return;
  var body = modal.querySelector('[data-invoice-modal-body]');
  var numberSlot = modal.querySelector('[data-invoice-modal-no]');
  var printButton = modal.querySelector('[data-invoice-modal-print]');
  var veil = document.querySelector('[data-scr-modal-veil="invoice-print"]');
  var request = 0;

  function state(text) {
    var box = document.createElement('div');
    box.className = 'bill-state';
    box.textContent = text;
    body.replaceChildren(box);
  }

  function open(url, number) {
    var mine = ++request;
    numberSlot.textContent = number || '';
    printButton.disabled = true;
    state('Loading the invoice…');
    if (veil) veil.hidden = false;
    modal.hidden = false;

    fetch(url, { headers: { 'X-Requested-With': 'fetch' }, credentials: 'same-origin' })
      .then(function (response) {
        if (!response.ok) throw new Error(response.status);
        return response.text();
      })
      .then(function (html) {
        if (mine !== request) return;          // a later click won
        body.innerHTML = html;
        printButton.disabled = false;
      })
      .catch(function () {
        if (mine === request) state('Could not load the invoice. Close and try again.');
      });
  }

  document.addEventListener('click', function (event) {
    var trigger = event.target.closest('[data-invoice-receipt]');
    if (!trigger) return;
    // A click on the row's own menu or a link inside the row is not a
    // click on the row -- only the menu's Print item opens the preview.
    var onRow = trigger.tagName === 'TR';
    if (onRow && event.target.closest('a, button, .scr-menu-wrap, .scr-menu')) return;
    event.preventDefault();
    open(trigger.dataset.invoiceReceipt, trigger.dataset.invoiceNo);
  });
})();

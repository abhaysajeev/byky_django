/* Invoice print preview (Rental > Invoices).

   One modal per page ([data-invoice-modal], rental/partials/
   invoice_print_modal.html). Anything carrying data-invoice-receipt="<url>"
   -- a list row, the row menu's Print item, the detail page's Print button --
   loads that invoice's receipt and shows it.

   While it loads only the veil shows, with a small spinner: the modal opens
   once, already holding the receipt -- never empty first. Clicking the veil
   or pressing Escape while it loads cancels. Print itself is
   byky-screen.js's [data-scr-print] (window.print); the receipt's own
   stylesheet puts only the receipt on paper. Closing the open modal (X,
   Close, veil, Escape) is byky-screen.js's too. */
(function () {
  'use strict';

  var modal = document.querySelector('[data-invoice-modal]');
  if (!modal) return;
  var body = modal.querySelector('[data-invoice-modal-body]');
  var numberSlot = modal.querySelector('[data-invoice-modal-no]');
  var printButton = modal.querySelector('[data-invoice-modal-print]');
  var veil = document.querySelector('[data-scr-modal-veil="invoice-print"]');
  var loader = veil && veil.querySelector('[data-invoice-loader]');
  var request = 0;          // the latest load; an older or cancelled one is ignored
  var loading = false;

  function stopLoading() {
    loading = false;
    if (loader) loader.hidden = true;
  }

  function cancel() {
    if (!loading) return;
    request += 1;
    stopLoading();
    if (veil) veil.hidden = true;
  }

  function show(content, printable) {
    stopLoading();
    body.replaceChildren();
    if (typeof content === 'string') {
      body.innerHTML = content;
    } else {
      body.appendChild(content);
    }
    printButton.disabled = !printable;
    body.scrollTop = 0;
    modal.hidden = false;
  }

  function failure() {
    var box = document.createElement('div');
    box.className = 'bill-state';
    box.textContent = 'Could not load it. Close and try again.';
    return box;
  }

  function open(url, number) {
    if (loading) return;    // one load at a time; a double click is one click
    var mine = ++request;
    loading = true;
    numberSlot.textContent = number || '';
    modal.hidden = true;
    if (veil) veil.hidden = false;
    if (loader) loader.hidden = false;

    fetch(url, { headers: { 'X-Requested-With': 'fetch' }, credentials: 'same-origin' })
      .then(function (response) {
        if (!response.ok) throw new Error(response.status);
        return response.text();
      })
      .then(function (html) {
        if (mine === request) show(html, true);
      })
      .catch(function () {
        if (mine === request) show(failure(), false);
      });
  }

  document.addEventListener('click', function (event) {
    var trigger = event.target.closest('[data-invoice-receipt]');
    if (!trigger) return;
    // A click on the row's own menu or a link inside the row is not a
    // click on the row -- only the menu's Print item opens the preview.
    if (trigger.tagName === 'TR' && event.target.closest('a, button, .scr-menu-wrap, .scr-menu')) return;
    event.preventDefault();
    open(trigger.dataset.invoiceReceipt, trigger.dataset.invoiceNo);
  });

  // While loading, the veil and Escape cancel instead of closing a modal.
  if (veil) veil.addEventListener('click', cancel);
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') cancel();
  });
})();

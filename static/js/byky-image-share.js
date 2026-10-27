/* Image Sharing (apps/fleet/templates/fleet/image_share_list.html). A photo
   (or the row's view button) opens one shared dialog filled with that share;
   Delete asks once, then posts -- apps/fleet/image_shares.py::delete. */
(function () {
  'use strict';

  var root = document.querySelector('[data-image-shares]');
  if (!root) return;

  var trigger = root.querySelector('[data-share-modal-trigger]');
  var img = root.querySelector('[data-share-img]');
  var deleteBtn = root.querySelector('[data-share-delete]');
  var current = null;

  function open(thumb) {
    current = thumb;
    img.src = thumb.dataset.full;
    root.querySelector('[data-share-station]').textContent = thumb.dataset.station;
    root.querySelector('[data-share-shared]').textContent = thumb.dataset.shared;
    root.querySelector('[data-share-remarks]').textContent = thumb.dataset.remarks;
    root.querySelector('[data-share-received]').textContent = 'Received ' + thumb.dataset.received;
    trigger.click();
  }

  root.querySelectorAll('[data-share-open]').forEach(function (thumb) {
    thumb.addEventListener('click', function () { open(thumb); });
  });
  root.querySelectorAll('[data-share-open-row]').forEach(function (btn) {
    btn.addEventListener('click', function () { open(btn.closest('tr').querySelector('[data-share-open]')); });
  });

  if (!deleteBtn) return;
  deleteBtn.addEventListener('click', function () {
    if (!current || deleteBtn.disabled) return;
    // Asked twice on purpose: the photo is no longer shown anywhere after this.
    if (deleteBtn.dataset.armed !== '1') {
      deleteBtn.dataset.armed = '1';
      deleteBtn.textContent = 'Delete — sure?';
      return;
    }
    deleteBtn.disabled = true;
    window.BykyCrud.post(current.dataset.deleteUrl, {}).then(function (result) {
      var body = result.body || {};
      if (body.ok) {
        window.BykyCrud.toastAfterReload(body.message);
        window.location.reload();
        return;
      }
      deleteBtn.disabled = false;
      window.BykyCrud.showMessages({ title: 'Could not delete',
        items: body.errors || [{ field: '', message: 'Something went wrong.' }] });
    });
  });
  // A new photo opened: the confirmation starts over.
  root.querySelectorAll('[data-share-open], [data-share-open-row]').forEach(function (el) {
    el.addEventListener('click', function () {
      deleteBtn.dataset.armed = '';
      deleteBtn.textContent = 'Delete';
    });
  });
})();

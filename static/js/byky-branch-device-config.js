/*
 * Branch-Device Config (owner, 5 Nov 2026): For Rent / For Receive per
 * operator tablet at a multi-device branch.
 *
 * Picking a branch reloads the page for it (?branch=<id>). Flipping a switch
 * saves that one flag at once; on a refusal the switch goes back and the
 * reason is shown. Posting and the toast are byky-crud.js's (window.BykyCrud).
 */
(function () {
  'use strict';

  var root = document.querySelector('[data-branch-device-config]');
  if (!root) return;
  var Crud = window.BykyCrud || {};
  var saveUrl = root.dataset.saveUrl;              // .../branch-device-config/0/save/

  var branchSelect = root.querySelector('[data-branch-select]');
  if (branchSelect) {
    branchSelect.addEventListener('change', function () {
      var query = new URLSearchParams(window.location.search);
      query.set('branch', branchSelect.value);
      window.location.search = '?' + query.toString();
    });
  }

  function setSwitch(button, on) {
    button.classList.toggle('is-on', on);
    button.setAttribute('aria-checked', on ? 'true' : 'false');
  }

  root.addEventListener('click', function (e) {
    var button = e.target.closest('.scr-toggle[data-flag]');
    if (!button || button.disabled) return;
    var row = button.closest('[data-pk]');
    if (!row || !Crud.post) return;

    var on = !button.classList.contains('is-on');
    var payload = {};
    payload[button.dataset.flag] = on;
    setSwitch(button, on);
    button.disabled = true;

    Crud.post(saveUrl.replace('/0/', '/' + row.dataset.pk + '/'), payload).then(function (result) {
      button.disabled = false;
      if (result.body.ok) {
        if (Crud.toast) Crud.toast(result.body.message);
        return;
      }
      setSwitch(button, !on);
      if (Crud.showMessages) {
        Crud.showMessages({
          title: result.status === 403 ? 'Not allowed' : 'Not saved',
          items: result.body.errors || [{ field: '', message: 'Something went wrong.' }],
        });
      }
    });
  });
})();

/*
 * The Tracking screens (design 07 section 7). One small script, driven by data
 * attributes:
 *
 *   data-tracking-filter        a select that reloads the page with ?<name>=value
 *   data-modal-open="name"      opens a modal, filling its fields from data-fill-*
 *   data-tracking-form          a form posted as JSON to its data-url
 *   data-post="url"             a button that posts; data-confirm asks first
 *   data-tracking-toggle        a switch posting {enabled: true|false}
 *   data-tracking-direction     an antenna direction select
 *   data-tracking-settings      the settings table, saved whole
 *   data-tracking-live          swapped from a fresh copy of the page on a change
 *
 * Posting and messages are byky-crud.js's (window.BykyCrud).
 */
(function () {
  'use strict';

  var Crud = window.BykyCrud || {};

  function fail(result) {
    if (!Crud.showMessages) return;
    Crud.showMessages({
      title: result.status === 403 ? 'Not allowed' : 'Not saved',
      items: result.body.errors || [{ field: '', message: 'Something went wrong.' }],
    });
  }

  function send(url, payload, button) {
    if (button) button.disabled = true;
    return Crud.post(url, payload).then(function (result) {
      if (button) button.disabled = false;
      if (result.body.ok) {
        if (Crud.toastAfterReload) Crud.toastAfterReload(result.body.message);
        window.location.reload();
      } else {
        fail(result);
      }
      return result;
    });
  }

  function modal(name, show) {
    var box = document.querySelector('[data-scr-modal="' + name + '"]');
    var veil = document.querySelector('[data-scr-modal-veil="' + name + '"]');
    if (!box) return null;
    box.hidden = !show;
    if (veil) veil.hidden = !show;
    return box;
  }

  function toPayload(form) {
    var payload = {};
    new FormData(form).forEach(function (value, key) { payload[key] = value; });
    return payload;
  }

  document.addEventListener('change', function (e) {
    var filter = e.target.closest('[data-tracking-filter]');
    if (filter) {
      var query = new URLSearchParams(window.location.search);
      if (filter.value) query.set(filter.name, filter.value); else query.delete(filter.name);
      query.delete('page');
      window.location.search = '?' + query.toString();
      return;
    }
    var direction = e.target.closest('[data-tracking-direction]');
    if (direction) send(direction.dataset.url, { port: direction.dataset.port, direction: direction.value });
  });

  document.addEventListener('click', function (e) {
    var opener = e.target.closest('[data-modal-open]');
    if (opener) {
      var box = modal(opener.dataset.modalOpen, true);
      if (box) {
        var form = box.querySelector('form');
        if (form) form.reset();
        if (form && opener.dataset.url) form.dataset.url = opener.dataset.url;
        Object.keys(opener.dataset).forEach(function (key) {
          if (key.indexOf('fill') !== 0) return;
          var name = key.slice(4).toLowerCase();
          var target = box.querySelector('[data-show="' + name + '"]');
          if (target) target.textContent = opener.dataset[key];
          var field = box.querySelector('[name="' + name + '"]');
          if (field) field.value = opener.dataset[key];
        });
      }
      return;
    }

    var closer = e.target.closest('[data-tracking-close]');
    if (closer) {
      modal(closer.closest('[data-scr-modal]').dataset.scrModal, false);
      return;
    }

    var poster = e.target.closest('[data-post]');
    if (poster) {
      if (poster.dataset.confirm && !window.confirm(poster.dataset.confirm)) return;
      send(poster.dataset.post, {}, poster);
      return;
    }

    var toggle = e.target.closest('[data-tracking-toggle]');
    if (toggle && !toggle.disabled) {
      send(toggle.dataset.url, { enabled: !toggle.classList.contains('is-on') }, toggle);
      return;
    }

    var alertsSwitch = e.target.closest('[data-alerts-switch]');
    if (alertsSwitch && !alertsSwitch.disabled) {
      var on = !alertsSwitch.classList.contains('is-on');
      alertsSwitch.classList.toggle('is-on', on);
      alertsSwitch.setAttribute('aria-checked', on ? 'true' : 'false');
      return;
    }

    var save = e.target.closest('[data-settings-save]');
    if (save) {
      var root = document.querySelector('[data-tracking-settings]');
      var read = function (row, name) {
        var input = row.querySelector('[name="' + name + '"]');
        return input ? input.value : '';
      };
      var fields = ['alert_after_s', 'missing_after_s', 'offline_after_s'];
      var pick = function (row) {
        var out = {};
        fields.forEach(function (f) { out[f] = read(row, f); });
        return out;
      };
      var payload = {
        retention_days: read(root, 'retention_days'),
        default: pick(root.querySelector('[data-default-row]')),
        branches: Array.prototype.map.call(root.querySelectorAll('[data-branch-row]'), function (row) {
          var values = pick(row);
          values.branch = row.dataset.branchRow;
          values.alerts_on = row.querySelector('[data-alerts-switch]').classList.contains('is-on');
          return values;
        }),
      };
      send(root.dataset.saveUrl, payload, save);
    }
  });

  document.addEventListener('submit', function (e) {
    var form = e.target.closest('[data-tracking-form]');
    if (!form) return;
    e.preventDefault();
    send(form.dataset.url, toPayload(form), form.querySelector('[type="submit"]'));
  });

  /* -- pages that keep themselves current ---------------------------------- */
  var live = document.querySelector('[data-tracking-live]');
  if (!live) return;

  function refresh() {
    fetch(window.location.href, { credentials: 'same-origin' }).then(function (r) {
      return r.ok ? r.text() : null;
    }).then(function (html) {
      if (!html) return;
      var fresh = new DOMParser().parseFromString(html, 'text/html').querySelector('[data-tracking-live]');
      if (fresh) live.innerHTML = fresh.innerHTML;
    }).catch(function () { /* the next tick tries again */ });
  }

  var pending = null;
  function soon() {
    clearTimeout(pending);
    pending = setTimeout(refresh, 500);
  }

  function listen(delay) {
    try {
      var socket = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/tracking/');
      socket.onmessage = soon;
      socket.onopen = function () { delay = 2000; };
      socket.onclose = function () { setTimeout(function () { listen(Math.min(delay * 2, 60000)); }, delay); };
    } catch (e) { /* the poll below carries on */ }
  }
  listen(2000);
  setInterval(refresh, 60000);
})();

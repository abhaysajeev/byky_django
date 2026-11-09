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
 *   data-test-connection        Test connection (Readers)
 *   data-bike-picker            search and pick bikes (calibration)
 *   data-calibration-status     polls a running calibration
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
    var chips = form.querySelectorAll('[data-bike-chip]');
    if (chips.length || form.querySelector('[data-bike-picker]')) {
      payload.vehicles = Array.prototype.map.call(chips, function (chip) { return chip.dataset.bikeChip; });
    }
    return payload;
  }

  /* -- Test connection: we cannot dial a reader, so this shows what we have
     heard from it, asking again every 5 s for a minute so someone at the
     station can restart the reader and watch it turn green. ---------------- */
  var TEST_EVERY_MS = 5000;
  var TEST_TRIES = 12;
  var testRun = 0;

  function testUrl(device) {
    var holder = document.querySelector('[data-test-url]');
    return holder.dataset.testUrl + '?device_id=' + encodeURIComponent(device);
  }

  function check(box, good, label, text) {
    var row = document.createElement('div');
    row.className = 'scr-track-check';
    var badge = document.createElement('span');
    badge.className = 'scr-badge ' + (good === null ? 'scr-badge-pending' : good ? 'scr-badge-active' : 'scr-badge-missing');
    badge.textContent = label;
    var detail = document.createElement('span');
    detail.textContent = text;
    row.appendChild(badge);
    row.appendChild(detail);
    box.appendChild(row);
  }

  function ago(seconds) {
    return seconds === null || seconds === undefined ? 'never' : seconds + ' s ago';
  }

  function showReport(box, data, trying) {
    box.hidden = false;
    box.innerHTML = '';
    var head = document.createElement('div');
    head.className = 'scr-track-headline';
    head.textContent = data.healthy ? 'Healthy: this reader is connected and sending tags.'
      : trying ? 'Not healthy yet. Checking again...' : 'Not healthy.';
    box.appendChild(head);

    var gateway = data.gateway;
    check(box, gateway.ok, 'Gateway', gateway.ok ? 'Running, in ' + gateway.mode + ' mode.'
      : gateway.running ? 'Running, but switched off or its database is not answering.'
      : 'Not running on this server, so the problem is on our side.');

    var reader = data.reader;
    check(box, data.connected, 'Reader', data.connected
      ? 'Heard ' + ago(Math.min.apply(null, [reader.heartbeat_age, reader.data_age].filter(function (a) { return a !== null; })))
        + (reader.ip ? ' from ' + reader.ip : '') + (reader.firmware ? ', firmware ' + reader.firmware : '') + '.'
      : reader.known ? 'Last heard: heartbeat ' + ago(reader.heartbeat_age) + '. Check its power, SIM, IP and port.'
      : 'Never heard from this device ID. Check the ID, the SIM, and the IP and port set on the reader.');

    check(box, data.sending, 'Tag data', data.sending ? 'Last upload ' + ago(reader.data_age) + '.'
      : 'No recent tag uploads (' + ago(reader.data_age) + '). Check the antennas and that tagged bikes are nearby.');
  }

  function runTest(device, box) {
    var run = ++testRun;
    var tries = 0;
    box.hidden = false;
    box.textContent = 'Checking...';

    function once() {
      if (run !== testRun) return;
      tries++;
      fetch(testUrl(device), { credentials: 'same-origin' }).then(function (r) {
        return r.ok ? r.json() : null;
      }).then(function (data) {
        if (run !== testRun) return;
        if (!data) {
          box.textContent = 'You do not have permission to test this reader.';
          return;
        }
        var again = !data.healthy && tries < TEST_TRIES;
        showReport(box, data, again);
        if (again) setTimeout(once, TEST_EVERY_MS);
      }).catch(function () {
        if (run === testRun) box.textContent = 'Could not reach the server.';
      });
    }
    once();
  }

  /* -- pick the bikes for a calibration --------------------------------------- */
  var picker = document.querySelector('[data-bike-picker]');
  if (picker) {
    var source = document.getElementById('bike-options');
    var options = source ? JSON.parse(source.textContent) : [];
    var input = picker.querySelector('input');
    var list = picker.querySelector('.scr-combo-list');
    var chips = document.querySelector('[data-bike-chips]');

    var chosen = function () {
      return Array.prototype.map.call(chips.querySelectorAll('[data-bike-chip]'), function (c) { return c.dataset.bikeChip; });
    };

    var add = function (bike) {
      var chip = document.createElement('span');
      chip.className = 'scr-track-chip';
      chip.dataset.bikeChip = bike.id;
      chip.appendChild(document.createTextNode(bike.name + ' '));
      var tag = document.createElement('small');
      tag.textContent = bike.tag;
      chip.appendChild(tag);
      var remove = document.createElement('button');
      remove.type = 'button';
      remove.setAttribute('aria-label', 'Remove ' + bike.name);
      remove.textContent = '×';
      remove.addEventListener('click', function () { chip.remove(); });
      chip.appendChild(remove);
      chips.appendChild(chip);
      input.value = '';
      list.hidden = true;
      input.focus();
    };

    var matches = function () {
      var q = input.value.trim().toLowerCase();
      var taken = chosen();
      return options.filter(function (b) {
        return taken.indexOf(String(b.id)) === -1
          && (!q || b.name.toLowerCase().indexOf(q) !== -1 || b.tag.toLowerCase().indexOf(q) !== -1);
      }).slice(0, 30);
    };

    var draw = function () {
      list.innerHTML = '';
      matches().forEach(function (bike) {
        var item = document.createElement('div');
        item.className = 'scr-combo-item';
        var main = document.createElement('span');
        main.className = 'scr-combo-item-main';
        main.textContent = bike.name;
        var sub = document.createElement('span');
        sub.className = 'scr-combo-item-sub';
        sub.textContent = bike.tag;
        item.appendChild(main);
        item.appendChild(sub);
        item.addEventListener('mousedown', function (e) { e.preventDefault(); add(bike); });
        list.appendChild(item);
      });
      list.hidden = !list.children.length;
    };

    input.addEventListener('input', draw);
    input.addEventListener('focus', draw);
    input.addEventListener('blur', function () { list.hidden = true; });
    input.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter') return;
      e.preventDefault();                  // Enter picks; only Start starts
      var first = matches()[0];
      if (first) add(first);
    });
  }

  /* -- a running calibration ---------------------------------------------------- */
  var running = document.querySelector('[data-calibration-status]');
  if (running) {
    setInterval(function () {
      if (document.hidden) return;
      fetch(running.dataset.url, { credentials: 'same-origin' }).then(function (r) {
        return r.ok ? r.json() : null;
      }).then(function (data) {
        if (!data) return;
        if (data.closed) { window.location.reload(); return; }
        running.querySelector('[data-cal-elapsed]').textContent = data.elapsed;
        data.bikes.forEach(function (bike) {
          var row = running.querySelector('[data-tag="' + bike.tag + '"]');
          if (!row) return;
          row.querySelector('[data-uploads]').textContent = bike.uploads;
          var badge = row.querySelector('[data-enough] .scr-badge');
          badge.className = 'scr-badge ' + (bike.enough ? 'scr-badge-active' : 'scr-badge-pending');
          badge.lastChild.textContent = bike.enough ? 'Enough' : 'Listening';
        });
        var finish = running.querySelector('[data-cal-finish]');
        if (finish) finish.disabled = !data.ready;
      }).catch(function () { /* the next poll tries again */ });
    }, 10000);
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
        var result = box.querySelector('[data-test-result]');
        if (result) { result.hidden = true; result.innerHTML = ''; }
        Object.keys(opener.dataset).forEach(function (key) {
          if (key.indexOf('fill') !== 0) return;
          var name = key.slice(4).toLowerCase();
          var target = box.querySelector('[data-show="' + name + '"]');
          if (target) target.textContent = opener.dataset[key];
          var field = box.querySelector('[name="' + name + '"]');
          if (field) field.value = opener.dataset[key];
        });
        if (box.hasAttribute('data-auto-test')) runTest(opener.dataset.fillDevice, box.querySelector('[data-test-result]'));
      }
      return;
    }

    var tester = e.target.closest('[data-test-connection]');
    if (tester) {
      var testForm = tester.closest('form');
      var device = testForm.querySelector('[name="device_id"]').value.trim();
      var testBox = testForm.querySelector('[data-test-result]');
      if (!device) {
        testBox.hidden = false;
        testBox.textContent = 'Enter the device ID first.';
        return;
      }
      runTest(device, testBox);
      return;
    }

    var closer = e.target.closest('[data-tracking-close]');
    if (closer) {
      testRun++;                           // stop any test still repeating
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
      // byky-screen.js flips every .scr-toggle on this page; only say what it did
      setTimeout(function () {
        alertsSwitch.setAttribute('aria-checked', alertsSwitch.classList.contains('is-on') ? 'true' : 'false');
      }, 0);
      return;
    }

    var save = e.target.closest('[data-settings-save]');
    if (save) {
      var root = document.querySelector('[data-tracking-settings]');
      var read = function (row, name) {
        var input = row.querySelector('[name="' + name + '"]');
        return input ? input.value : '';
      };
      var fields = ['missing_after_s', 'offline_after_s'];
      var pick = function (row) {
        var out = {};
        fields.forEach(function (f) { out[f] = read(row, f); });
        return out;
      };
      var payload = {
        retention_days: read(root, 'retention_days'),
        allowance_out_db: read(root, 'allowance_out_db'),
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
    if (document.hidden) return;
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
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) refresh();          // catch up on what a hidden tab skipped
  });
})();

/* Rental -> Live Monitoring (rental/live_monitor.html, apps/rental/live.py).

   The page keeps the day's orders current by itself:
   - a websocket (/ws/live/) says which order changed; the row is fetched back
     over plain HTTP (live/rows/?ids=), so the server alone decides what a user
     may see and how it reads;
   - after a dropped connection the whole list is fetched again, so nothing
     missed while away is lost;
   - rows being read are never moved: scrolled down, updates wait behind a
     "N updates" pill. Wall view (for a TV) applies them at once -- nobody is
     there to click;
   - a row opens its order's detail page (Ctrl/Cmd-click: a new tab).
   A past day (?day=) is a static look back: no socket. */
(function () {
  'use strict';

  var root = document.querySelector('[data-live]');
  if (!root) return;

  var rowsUrl = root.dataset.rowsUrl;
  var isToday = root.dataset.today === '1';
  var host = root.querySelector('[data-live-rows]');
  var pill = root.querySelector('[data-live-pill]');
  var stateEl = root.querySelector('[data-live-state]');
  var form = root.querySelector('[data-live-filters]');

  var initial = JSON.parse(document.getElementById('live-initial').textContent);
  var rows = {};                                   // id -> row
  var held = {};                                   // id -> 'new' | 'changed', waiting behind the pill
  var tab = 'rent';
  var stopped = false;                             // signed out / not allowed: no more fetching

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function dur(ms) {
    var m = Math.max(0, Math.floor(ms / 60000));
    return m < 60 ? m + ' min' : Math.floor(m / 60) + 'h ' + pad(m % 60) + 'm';
  }
  function ago(iso) {
    if (!iso) return '';
    var ms = Date.now() - new Date(iso).getTime();
    return ms < 50000 ? 'just now' : dur(ms) + ' ago';
  }
  function walled() { return document.body.classList.contains('scr-live-wall'); }

  // ---- filters ------------------------------------------------------------------
  function params() {
    var p = new URLSearchParams();
    p.set('tab', tab);
    form.querySelectorAll('select[name], input[name="q"]').forEach(function (el) {
      if (el.value) p.set(el.name, el.value.trim());
    });
    if (!isToday) p.set('day', root.dataset.day);
    return p;
  }

  // ---- rows ---------------------------------------------------------------------
  // One table row per order -- the legacy grid's columns (owner, 28 Oct 2026);
  // below 768px the CSS reads each row as a card, using the data-label cells.
  var COLS = 14;
  var open = {};                                   // id -> true: its vehicles are shown

  function money(v) { return v == null ? '<span class="scr-live-nil">—</span>' : esc(v); }

  function timing(r) {
    var second = '';
    if (r.status === 'active') {
      var now = Date.now(), due = r.due ? new Date(r.due).getTime() : null;
      second = due && now > due
        ? '<span class="scr-live-late">Over by ' + dur(now - due) + '</span>'
        : 'Running ' + dur(now - new Date(r.start).getTime());
    } else if (r.status === 'completed' && r.closed_at) {
      second = 'Ran ' + dur(new Date(r.closed_at).getTime() - new Date(r.start).getTime());
    }
    return '<div class="scr-live-p scr-live-num">' + esc(r.start_label) + '</div>' +
      (second ? '<div class="scr-live-q scr-live-num">' + second + '</div>' : '');
  }

  function cell(cls, label, inner) {
    return '<td class="' + cls + '" data-label="' + label + '">' + inner + '</td>';
  }

  function vehicleRow(r) {
    var chips = r.vehicles.map(function (v) {
      var when = v.state === 'out' ? (v.due ? 'due ' + v.due : 'out') : v.end ? v.start + '–' + v.end : v.start;
      return '<span class="scr-live-chip is-' + esc(v.state) + '"><i></i>' + esc(v.no) +
        '<em>' + esc(v.state) + ' · ' + esc(when) + '</em></span>';
    }).join('');
    return '<tr class="scr-live-veh-row" data-veh-for="' + esc(r.id) + '"' + (open[r.id] ? '' : ' hidden') + '>' +
      '<td colspan="' + COLS + '"><div class="scr-live-veh">' + (chips || '<span class="scr-live-nil">No vehicles</span>') +
      '</div></td></tr>';
  }

  function rowHtml(r) {
    var n = r.vehicles.length;
    // The whole row opens the order; this opens its vehicles.
    var bike = '<button type="button" class="scr-live-veh-btn' + (open[r.id] ? ' is-on' : '') +
      '" data-live-veh="' + esc(r.id) + '" title="Show vehicles" aria-label="Show vehicles of ' + esc(r.order_no) +
      '" aria-expanded="' + (open[r.id] ? 'true' : 'false') + '">' +
      // The owner's bicycle (bike-svgrepo-com.svg, 28 Oct 2026), in black; its
      // light tubes a soft grey. viewBox cropped to the drawing.
      '<svg width="20" height="16" viewBox="196 246 666 532" aria-hidden="true"><path d="M354.614054 430.364318l67.91349 138.460135 33.964581-23.510324-47.020648-112.332328zM636.737943 404.252185l78.367746 222.0315 39.183874-10.438584-67.91349-235.10324z" fill="#111" fill-opacity="0.16"></path><path d="M335.022117 767.345629c-70.577993 0-128.005878-57.427885-128.005877-128.005877s57.427885-128.005878 128.005877-128.005878c3.745978 0 7.460609 0.172409 11.112547 0.470207 8.620452 0.736657 15.01526 8.338328 14.294277 16.95878-0.736657 8.620452-8.369675 14.999587-16.95878 14.294277a96.784167 96.784167 0 0 0-105.106822 96.298287c0 53.290068 43.353038 96.658779 96.658778 96.658779a96.75282 96.75282 0 0 0 95.326528-112.755514 15.673549 15.673549 0 1 1 30.908239-5.172271c1.175516 6.974729 1.755438 14.121868 1.755437 21.269006 0 70.577993-57.412211 127.990204-127.990204 127.990204zM709.870724 767.345629c-76.345859 0-138.444461-62.114276-138.444462-138.444461a137.660784 137.660784 0 0 1 53.384109-109.244639 15.657876 15.657876 0 1 1 19.262793 24.717187 106.501768 106.501768 0 0 0-41.299803 84.527452 107.207078 107.207078 0 0 0 107.097363 107.097363 107.207078 107.207078 0 0 0 107.097363-107.097363 107.066016 107.066016 0 0 0-47.240078-88.822004 15.673549 15.673549 0 0 1 17.554375-25.986745 138.397441 138.397441 0 0 1 61.032801 114.793075c0.015674 76.361532-62.098603 138.460135-138.444461 138.460135z" fill="#111"></path><path d="M418.609156 591.347344a15.689223 15.689223 0 0 1-14.498033-9.733275l-56.017265-136.877106h-27.444385a15.673549 15.673549 0 0 1 0-31.347099h128.005877a15.673549 15.673549 0 0 1 0 31.347099h-10.987158l36.942556 94.276399a15.673549 15.673549 0 0 1-5.219292 18.275359l-41.409517 30.95526a15.642202 15.642202 0 0 1-9.372783 3.103363z m-36.644758-146.610381l43.478426 106.266665 15.626528-11.676795-37.067944-94.58987H381.964398z" fill="#111"></path><path d="M335.037791 655.028975a15.657876 15.657876 0 0 1-9.1847-28.384798l309.5526-223.348079a15.689223 15.689223 0 0 1 18.338053 25.422498l-309.5526 223.348078a15.657876 15.657876 0 0 1-9.153353 2.962301zM613.227619 345.476375h-78.367747a15.673549 15.673549 0 0 1 0-31.347099h78.367747a15.673549 15.673549 0 0 1 0 31.347099z" fill="#111"></path><path d="M715.105689 651.110587a15.610855 15.610855 0 0 1-14.968239-11.049852l-108.946842-352.059266h-103.382731a15.673549 15.673549 0 0 1 0-31.347098h114.934137a15.673549 15.673549 0 0 1 14.96824 11.049852l34.168337 110.404482 30.829872-10.281849a15.736244 15.736244 0 0 1 19.905407 10.140787l74.44936 235.10324a15.673549 15.673549 0 0 1-9.435477 19.403854l-47.020648 17.632743a15.45412 15.45412 0 0 1-5.501416 1.003107z m-53.948357-243.04973l63.995102 206.859505 17.538702-6.567217-65.154944-205.746683-16.37886 5.454395z" fill="#111"></path></svg>' +
      '<span class="scr-live-veh-n">' + n + '</span><span class="scr-live-veh-t">' + (n === 1 ? 'vehicle' : 'vehicles') +
      '</span></button>';
    return '<tr class="scr-live-row is-' + esc(r.status) + '" data-id="' + esc(r.id) + '">' +
      cell('c-no', 'Order', '<a class="scr-live-no" href="' + esc(r.url) + '">' + esc(r.order_no) + '</a>' +
        '<div class="scr-live-q scr-live-num">' + (r.transaction_no ? esc(r.transaction_no) + ' · ' : '') +
        esc(r.booked_time) + ' · ' + esc(r.booked_date) + '</div>') +
      cell('c-station', 'Station', '<div class="scr-live-p">' + esc(r.station) + '</div><div class="scr-live-q">' + esc(r.emirate) + '</div>') +
      cell('c-staff', 'Open / Close by', '<div class="scr-live-q scr-live-staff"><b>In</b> ' + esc(r.opened_by) + '</div>' +
        '<div class="scr-live-q scr-live-staff"><b>Out</b> ' + esc(r.closed_by) + '</div>') +
      cell('c-cust', 'Customer', '<div class="scr-live-p">' + esc(r.customer) + '</div><div class="scr-live-q scr-live-num">' + esc(r.mobile) + '</div>') +
      cell('c-start', 'Start', timing(r)) +
      cell('c-amt c-collected', 'Collected', money(r.collected) +
        '<div class="scr-live-q scr-live-fold">Dep ' + money(r.deposit) + '</div>') +
      cell('c-amt c-fare', 'Fare', money(r.fare)) +
      cell('c-amt c-deposit', 'Deposit', money(r.deposit)) +
      cell('c-amt c-bill', 'Bill', money(r.bill)) +
      cell('c-amt c-tax', 'Tax', money(r.tax)) +
      cell('c-amt c-disc', 'Discount', money(r.discount)) +
      cell('c-amt c-net', 'Net', money(r.net) +
        (Number(r.discount) > 0 ? '<div class="scr-live-q scr-live-fold">Disc ' + money(r.discount) + '</div>' : '')) +
      cell('c-status', 'Status', '<span class="scr-live-badge is-' + esc(r.status) + '">' + esc(r.status_label) + '</span>' +
        '<div class="scr-live-q">' + esc(r.last) + (r.last_at ? ' · ' + ago(r.last_at) : '') + '</div>') +
      '<td class="c-act">' + bike + '</td>' +
      '</tr>' + vehicleRow(r);
  }

  function band(label, n) {
    return '<tr class="scr-live-group"><td colspan="' + COLS + '"><b>' + label + '</b>' + n + '</td></tr>';
  }

  function render(marks) {
    var all = Object.keys(rows).map(function (id) { return rows[id]; });
    var running = all.filter(function (r) { return r.status === 'active'; })
      .sort(function (a, b) { return b.start < a.start ? -1 : 1; });
    var closed = all.filter(function (r) { return r.status !== 'active'; })
      .sort(function (a, b) { return (b.last_at || '') < (a.last_at || '') ? -1 : 1; });
    var html = '';
    if (running.length) html += band('Running', running.length) + running.map(rowHtml).join('');
    if (closed.length) html += band('Closed', closed.length) + closed.map(rowHtml).join('');
    host.innerHTML = html || '<tr class="scr-live-empty-row"><td colspan="' + COLS + '"><div class="scr-live-empty">' +
      (isToday ? 'No orders yet today.' : 'No orders that day.') + '</div></td></tr>';
    Object.keys(marks || {}).forEach(function (id) {
      var el = host.querySelector('[data-id="' + id + '"]');
      if (el) el.classList.add(marks[id] === 'new' ? 'is-new' : 'is-changed');
    });
  }

  var lastKpis = null;
  function totals(payload) {
    var k = payload.kpis;
    Object.keys(k).forEach(function (key) {
      var tile = root.querySelector('[data-kpi="' + key + '"]');
      if (!tile) return;
      tile.querySelector('[data-kpi-val]').textContent = k[key];
      if (lastKpis && lastKpis[key] !== k[key]) {
        tile.classList.add('is-bump');
        setTimeout(function () { tile.classList.remove('is-bump'); }, 1200);
      }
    });
    lastKpis = k;
    Object.keys(payload.counts).forEach(function (t) {
      var el = root.querySelector('[data-count="' + t + '"]');
      if (el) el.textContent = payload.counts[t];
    });
  }

  function replaceAll(payload) {
    rows = {};
    payload.rows.forEach(function (r) { rows[r.id] = r; });
    held = {};
    pill.hidden = true;
    totals(payload);
    render();
  }

  // ---- fetching -----------------------------------------------------------------
  function get(url) {
    return fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } }).then(function (res) {
      if (res.status === 401 || res.status === 403) {
        stopped = true;
        show(res.status === 401 ? 'Signed out' : 'Not allowed', false);
        if (socket) socket.close();
        throw new Error('stopped');
      }
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return res.json();
    });
  }

  var refreshTimer = null;
  function reload() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(function () {
      if (stopped) return;
      get(rowsUrl + '?' + params().toString()).then(replaceAll).catch(function () {});
    }, 200);
  }

  // Pushed ids are gathered for 300 ms, then fetched in one call.
  var pending = {}, pendingTimer = null;
  function changed(id) {
    pending[id] = true;
    clearTimeout(pendingTimer);
    pendingTimer = setTimeout(flush, 300);
  }
  function flush() {
    var ids = Object.keys(pending);
    pending = {};
    if (!ids.length || stopped) return;
    var p = params();
    p.set('ids', ids.join(','));
    get(rowsUrl + '?' + p.toString()).then(function (payload) {
      var marks = {}, found = {};
      payload.rows.forEach(function (r) {
        found[r.id] = true;
        marks[r.id] = rows[r.id] ? 'changed' : 'new';
        rows[r.id] = r;
      });
      ids.forEach(function (id) { if (!found[id]) delete rows[id]; });   // no longer matches the filters
      totals(payload);
      apply(marks);
    }).catch(function () {});
  }

  function reading() {                             // scrolled down into the list
    return host.getBoundingClientRect().top < -40;
  }
  function apply(marks) {
    if (!walled() && reading()) {
      Object.keys(marks).forEach(function (id) { held[id] = marks[id]; });
      var n = Object.keys(held).length;
      pill.textContent = n + ' update' + (n > 1 ? 's' : '') + ' ↑';
      pill.hidden = false;
      return;
    }
    render(marks);
  }
  pill.addEventListener('click', function () {
    var marks = held;
    held = {};
    pill.hidden = true;
    root.scrollIntoView({ behavior: 'smooth', block: 'start' });
    render(marks);
  });

  // ---- the websocket ------------------------------------------------------------
  var socket = null, tries = 0, everOpen = false;
  var BACKOFF = [1000, 2000, 5000, 10000];

  function show(text, on) {
    if (!stateEl) return;
    stateEl.classList.toggle('is-on', !!on);
    stateEl.lastElementChild.textContent = text;
  }

  function wsUrl() {
    if (root.dataset.wsUrl) return root.dataset.wsUrl;
    return (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/live/';
  }

  function connect() {
    if (stopped || !window.WebSocket) return;
    var opened = false;
    socket = new WebSocket(wsUrl());
    socket.onopen = function () {
      opened = true;
      show('Live', true);
      if (everOpen || tries) reload();             // back after a drop: catch up on what was missed
      everOpen = true;
      tries = 0;
    };
    socket.onmessage = function (e) {
      var msg;
      try { msg = JSON.parse(e.data); } catch (err) { return; }
      if (msg.type === 'order.changed' && msg.order) changed(msg.order);
    };
    socket.onclose = function (e) {
      socket = null;
      if (stopped) return;
      if (e.code === 4403) {                        // refused: signed out, or no permission
        stopped = true;
        show('Signed out', false);
        return;
      }
      // A refusal before the handshake reaches the browser as a bare 1006, not
      // 4403. Ask the rows endpoint: a 401/403 there stops the retries (get()).
      if (!opened) reload();
      show('Reconnecting…', false);
      setTimeout(connect, BACKOFF[Math.min(tries++, BACKOFF.length - 1)]);
    };
  }

  // ---- a row opens its order ----------------------------------------------------
  host.addEventListener('click', function (e) {
    var veh = e.target.closest('[data-live-veh]');
    if (veh) {                                               // View vehicles: open the row below
      var id = veh.dataset.liveVeh, on = !open[id];
      if (on) open[id] = true; else delete open[id];
      var line = host.querySelector('[data-veh-for="' + id + '"]');
      if (line) line.hidden = !on;
      veh.classList.toggle('is-on', on);
      veh.setAttribute('aria-expanded', on ? 'true' : 'false');
      return;
    }
    if (walled()) { e.preventDefault(); return; }
    if (e.target.closest('a')) return;                       // the order no and View order are real links
    var row = e.target.closest('.scr-live-row');
    var r = row && rows[row.dataset.id];
    if (!r) return;
    if (e.ctrlKey || e.metaKey) window.open(r.url, '_blank');
    else location.href = r.url;
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && walled()) setWall(false);
  });

  // ---- filters ------------------------------------------------------------------
  form.addEventListener('submit', function (e) { e.preventDefault(); });
  // Phones: the filters fold behind a button (CSS shows them from 768px).
  var filterToggle = form.querySelector('[data-live-filter-toggle]');
  if (filterToggle) filterToggle.addEventListener('click', function () {
    var on = form.classList.toggle('is-filters-open');
    filterToggle.setAttribute('aria-expanded', on ? 'true' : 'false');
  });
  root.querySelectorAll('[data-tab]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      tab = btn.dataset.tab;
      root.querySelectorAll('[data-tab]').forEach(function (b) { b.classList.toggle('is-on', b === btn); });
      reload();
    });
  });
  var stateSel = form.querySelector('[name="state"]'), branchSel = form.querySelector('[name="branch"]');
  stateSel.addEventListener('change', function () {   // the station list follows the emirate
    Array.prototype.forEach.call(branchSel.options, function (o) {
      o.hidden = !!(o.value && stateSel.value && o.dataset.state !== stateSel.value);
    });
    if (branchSel.selectedOptions[0] && branchSel.selectedOptions[0].hidden) branchSel.value = '';
  });
  form.querySelectorAll('select').forEach(function (el) { el.addEventListener('change', reload); });
  var typing = null;
  form.querySelector('[name="q"]').addEventListener('input', function () {
    clearTimeout(typing);
    typing = setTimeout(reload, 250);
  });
  form.querySelector('[name="day"]').addEventListener('change', function () {
    // Another day is another page: static, no socket.
    var url = new URL(location.href);
    if (this.value) url.searchParams.set('day', this.value); else url.searchParams.delete('day');
    location.href = url.toString();
  });

  // ---- wall view ----------------------------------------------------------------
  var wakeLock = null;
  function setWall(on) {
    document.body.classList.toggle('scr-live-wall', on);
    if (on && document.documentElement.requestFullscreen) document.documentElement.requestFullscreen().catch(function () {});
    if (!on && document.fullscreenElement) document.exitFullscreen().catch(function () {});
    if (on && navigator.wakeLock) {
      navigator.wakeLock.request('screen').then(function (lock) { wakeLock = lock; }).catch(function () {});
    } else if (!on && wakeLock) {
      wakeLock.release().catch(function () {});
      wakeLock = null;
    }
    if (!on && Object.keys(held).length) pill.click();
  }
  root.querySelector('[data-live-wall]').addEventListener('click', function () { setWall(true); });
  root.querySelector('[data-live-unwall]').addEventListener('click', function () { setWall(false); });
  document.addEventListener('visibilitychange', function () {   // a wake lock is dropped when the tab hides
    if (walled() && document.visibilityState === 'visible' && navigator.wakeLock && !wakeLock) {
      navigator.wakeLock.request('screen').then(function (lock) { wakeLock = lock; }).catch(function () {});
    }
  });

  // ---- clock and the running times ----------------------------------------------
  var clockT = root.querySelector('[data-live-clock]'), clockD = root.querySelector('[data-live-date]');
  function clock() {
    var d = new Date();
    clockT.textContent = pad(d.getHours()) + ':' + pad(d.getMinutes());
    clockD.textContent = d.toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' });
  }
  clock();
  setInterval(clock, 10000);
  setInterval(function () { if (!reading() || walled()) render(); }, 30000);

  replaceAll(initial);
  if (isToday) connect();
})();

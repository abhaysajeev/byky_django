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
  function vehicles(r) {
    var chips = r.vehicles.slice(0, 3).map(function (v) {
      return '<span class="scr-live-chip is-' + esc(v.state) + '"><i></i>' + esc(v.no) + '</span>';
    }).join('');
    if (r.vehicles.length > 3) chips += '<span class="scr-live-chip is-more">+' + (r.vehicles.length - 3) + '</span>';
    var line = r.status === 'cancelled' ? 'Cancelled'
      : r.out ? r.out + ' out' + (r.back ? ' · ' + r.back + ' back' : '')
      : r.back + ' returned';
    return '<div class="scr-live-veh">' + chips + '</div><div class="scr-live-q">' + line + '</div>';
  }

  function timing(r) {
    var second;
    if (r.status === 'active') {
      var now = Date.now(), due = r.due ? new Date(r.due).getTime() : null;
      second = due && now > due
        ? '<span class="scr-live-late">Over by ' + dur(now - due) + '</span>'
        : 'Running ' + dur(now - new Date(r.start).getTime());
    } else if (r.status === 'completed' && r.closed_at) {
      second = 'Ran ' + dur(new Date(r.closed_at).getTime() - new Date(r.start).getTime());
    } else {
      second = '—';
    }
    return '<div class="scr-live-p scr-live-num">' + esc(r.start_label) + '</div><div class="scr-live-q scr-live-num">' + second + '</div>';
  }

  function amount(r) {
    if (r.status === 'completed') {
      return '<div class="scr-live-p scr-live-num">' + esc(r.net) + '</div><div class="scr-live-q">Tax ' + esc(r.tax || '0.00') +
        (r.discount ? ' · Disc ' + esc(r.discount) : '') + '</div>';
    }
    if (r.status === 'cancelled') return '<div class="scr-live-p">—</div><div class="scr-live-q">Cancelled</div>';
    return '<div class="scr-live-p scr-live-num">' + esc(r.collected) + '</div><div class="scr-live-q">collected so far</div>';
  }

  function rowHtml(r) {
    return '<div class="scr-live-row is-' + esc(r.status) + '" data-id="' + esc(r.id) + '">' +
      '<div><div class="scr-live-p"><a class="scr-live-no" href="' + esc(r.url) + '">' + esc(r.order_no) + '</a></div><div class="scr-live-q">' + (r.transaction_no ? esc(r.transaction_no) + ' · ' : '') + esc(r.booked) + '</div></div>' +
      '<div><div class="scr-live-p">' + esc(r.station) + '</div><div class="scr-live-q">' + esc(r.emirate) + '</div></div>' +
      '<div><div class="scr-live-p">' + esc(r.customer) + '</div><div class="scr-live-q scr-live-num">' + esc(r.mobile) + '</div></div>' +
      '<div class="scr-live-c-veh">' + vehicles(r) + '</div>' +
      '<div>' + timing(r) + '</div>' +
      '<div><div class="scr-live-q"><b>In</b> ' + esc(r.opened_by) + '</div><div class="scr-live-q"><b>Out</b> ' + esc(r.closed_by) + '</div></div>' +
      '<div class="is-r">' + amount(r) + '</div>' +
      '<div class="scr-live-c-act"><span class="scr-live-badge is-' + esc(r.status) + '"><i></i>' + esc(r.status_label) + '</span>' +
      '<div class="scr-live-q">' + esc(r.last) + (r.last_at ? ' · ' + ago(r.last_at) : '') + '</div></div>' +
      '</div>';
  }

  function render(marks) {
    var all = Object.keys(rows).map(function (id) { return rows[id]; });
    var running = all.filter(function (r) { return r.status === 'active'; })
      .sort(function (a, b) { return b.start < a.start ? -1 : 1; });
    var closed = all.filter(function (r) { return r.status !== 'active'; })
      .sort(function (a, b) { return (b.last_at || '') < (a.last_at || '') ? -1 : 1; });
    var html = '';
    if (running.length) html += '<div class="scr-live-group"><b>Running</b>' + running.length + '</div>' + running.map(rowHtml).join('');
    if (closed.length) html += '<div class="scr-live-group"><b>Closed</b>' + closed.length + '</div>' + closed.map(rowHtml).join('');
    host.innerHTML = html || '<div class="scr-live-empty">' + (isToday ? 'No orders yet today.' : 'No orders that day.') + '</div>';
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
    if (walled()) { e.preventDefault(); return; }
    if (e.target.closest('a')) return;                       // the order no is a real link already
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

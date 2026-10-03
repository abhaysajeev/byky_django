/* BYKY dashboard — the parts of the design that must be computed from data:
   sparkline, month bars, emirate/category bars, deployment gauge, station map,
   plus the live first row (branch carousel, overdue watch) and its poller.
   Reads #byky-chart-data and #bd-live from the page. No vendor libraries. */
(function () {
  'use strict';

  var root = document.querySelector('.bd');
  if (!root) return;

  var node = document.getElementById('byky-chart-data');
  var data = node ? JSON.parse(node.textContent) : {};
  var fmt = function (n) { return Math.round(n).toLocaleString('en-US'); };
  var svgNS = 'http://www.w3.org/2000/svg';
  var el = function (name, attrs) {
    var n = document.createElementNS(svgNS, name);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  };
  var RED = '#d81f26', MID = '#ef767b', PALE = '#f6b8bb';

  /* ── live: branch carousel, overdue watch and one poller ─────── */
  var liveNode = document.getElementById('bd-live');
  var live = liveNode ? JSON.parse(liveNode.textContent) : null;
  var REDUCED = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  var SLIDE_MS = 7000;

  // Branch carousel: one slide per station, busiest first. The slides are
  // stacked in one grid cell and only two are ever painted: the one leaving
  // and the one arriving, moved by the browser's compositor. A new
  // busiest-first order is applied only when the loop comes back to the start.
  var carousel = (function () {
    var box = root.querySelector('[data-bd-carousel]');
    if (!box) return null;
    var track = box.querySelector('[data-bd-track]');
    var pos = box.querySelector('[data-bd-pos]');
    var i = 0, timer = null, hold = false, running = true, nextOrder = null, moving = [];
    var MOTION = { duration: 700, easing: 'cubic-bezier(0.33, 0, 0.2, 1)' };

    function slides() { return track.querySelectorAll('.bd-car-slide'); }
    function reorder(ids) {
      var byId = {};
      slides().forEach(function (s) { byId[s.getAttribute('data-branch')] = s; });
      ids.forEach(function (id) { if (byId[id]) track.appendChild(byId[id]); });
    }
    function go(n, dir) {
      var all = slides(), count = all.length;
      if (count < 2) return;
      moving.forEach(function (m) { m.finish(); });     // a quick second click completes the first
      moving = [];
      var from = all[i];
      if (n >= count && nextOrder) {
        reorder(nextOrder);
        nextOrder = null;
        all = slides();
        n = 0;
      }
      var target = ((n % count) + count) % count;
      var to = all[target];
      i = target;
      if (pos) pos.textContent = String(i + 1);
      if (to === from) return;
      to.classList.add('is-on');
      if (REDUCED || !from.animate) { from.classList.remove('is-on'); return; }
      // A short glide with motion blur: the leaving station blurs and fades as
      // it moves off, the arriving one sharpens into place.
      var still = { transform: 'translateX(0)', opacity: 1, filter: 'blur(0)' };
      var out = from.animate([still, {
        transform: 'translateX(' + (-32 * dir) + '%)', opacity: 0, filter: 'blur(8px)'
      }], MOTION);
      var into = to.animate([{
        transform: 'translateX(' + (32 * dir) + '%)', opacity: 0, filter: 'blur(8px)'
      }, still], MOTION);
      moving = [out, into];
      out.onfinish = function () { if (from !== slides()[i]) from.classList.remove('is-on'); };
    }
    function schedule() {
      clearTimeout(timer);
      if (running && !hold && !REDUCED && slides().length > 1) {
        timer = setTimeout(function () { go(i + 1, 1); schedule(); }, SLIDE_MS);
      }
    }
    function step(d) { go(i + d, d); schedule(); }

    var prev = box.querySelector('[data-bd-prev]'), next = box.querySelector('[data-bd-next]');
    if (prev) prev.addEventListener('click', function () { step(-1); });
    if (next) next.addEventListener('click', function () { step(1); });
    box.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowLeft') step(-1);
      if (e.key === 'ArrowRight') step(1);
    });
    box.addEventListener('mouseenter', function () { hold = true; schedule(); });
    box.addEventListener('mouseleave', function () { hold = false; schedule(); });
    box.addEventListener('focusin', function () { hold = true; schedule(); });
    box.addEventListener('focusout', function () { hold = false; schedule(); });

    function setText(el, value) {
      value = String(value);
      if (!el || el.textContent === value) return;
      el.textContent = value;
      if (el.classList.contains('bd-car-num')) {
        el.classList.add('is-changed');
        setTimeout(function () { el.classList.remove('is-changed'); }, 1200);
      }
    }
    function update(branches) {
      branches.forEach(function (b) {
        var slide = track.querySelector('[data-branch="' + b.id + '"]');
        if (!slide) return;
        ['hours', 'on_rent', 'invoices', 'revenue', 'devices', 'staff'].forEach(function (f) {
          setText(slide.querySelector('[data-live="' + f + '"]'), b[f]);
        });
        var chip = slide.querySelector('[data-live="state"]');
        if (chip) {
          chip.className = 'bd-car-state is-' + b.state;
          chip.textContent = b.state === 'open' ? 'Open' : 'Closed';
        }
      });
      var ids = branches.map(function (b) { return String(b.id); });
      var shown = Array.prototype.map.call(slides(), function (s) { return s.getAttribute('data-branch'); });
      nextOrder = ids.join() === shown.join() ? null : ids;
    }

    schedule();
    return {
      update: update,
      run: function (on) { running = on; schedule(); }
    };
  })();

  // Overdue watch: pages of 3 (what the card's height holds), swapped in place
  // with the carousel (SLIDE_MS) when there are more.
  // The minutes overdue are worked out here from each booked end time, so
  // they keep counting between refreshes without asking the server.
  var overdue = (function () {
    var box = root.querySelector('[data-bd-overdue]');
    if (!box || !live) return null;
    var list = box.querySelector('[data-bd-od-list]');
    var pageEl = box.querySelector('[data-bd-od-page]');
    var rows = live.overdue.rows || [];
    var page = 0, timer = null, hold = false, running = true;
    var PER_PAGE = 3;

    function minutes(iso) {
      var m = Math.max(1, Math.floor((Date.now() - Date.parse(iso)) / 60000));
      return m < 60 ? '+' + m + ' min' : '+' + Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
    }
    function span(cls, text) {
      var s = document.createElement('span');
      s.className = cls;
      s.textContent = text;
      return s;
    }
    function pages() { return Math.max(1, Math.ceil(rows.length / PER_PAGE)); }
    function render() {
      if (page >= pages()) page = 0;
      list.textContent = '';
      if (!rows.length) {
        var empty = document.createElement('li');
        empty.className = 'bd-od-empty';
        empty.textContent = 'No overdue vehicles';
        list.appendChild(empty);
      }
      rows.slice(page * PER_PAGE, page * PER_PAGE + PER_PAGE).forEach(function (r) {
        var a = document.createElement('a');
        a.className = 'bd-od-row';
        a.href = r.url;
        var main = span('bd-od-main', '');
        main.appendChild(span('bd-od-vehicle', r.vehicle));
        main.appendChild(span('bd-od-station', r.station));
        var who = span('bd-od-who', '');
        who.appendChild(span('bd-od-name', r.customer));
        who.appendChild(span('bd-od-phone', r.mobile));
        var late = span('bd-od-late', minutes(r.due));
        late.setAttribute('data-due', r.due);
        a.appendChild(main);
        a.appendChild(who);
        a.appendChild(late);
        var li = document.createElement('li');
        li.appendChild(a);
        list.appendChild(li);
      });
      pageEl.textContent = pages() > 1 ? (page + 1) + ' / ' + pages() : '';
    }
    function schedule() {
      clearTimeout(timer);
      if (running && !hold && pages() > 1) {
        timer = setTimeout(function () { page = (page + 1) % pages(); render(); schedule(); }, SLIDE_MS);
      }
    }
    function tick() {
      list.querySelectorAll('[data-due]').forEach(function (el) { el.textContent = minutes(el.getAttribute('data-due')); });
    }
    function update(data) {
      box.querySelectorAll('[data-live-overdue]').forEach(function (el) {
        el.textContent = String(data[el.getAttribute('data-live-overdue')]);
      });
      var count = box.querySelector('.bd-od-count');
      if (count) count.classList.toggle('is-late', data.overdue > 0);
      rows = data.rows || [];
      render();                                    // keeps the page when it still exists
      schedule();
    }

    box.addEventListener('mouseenter', function () { hold = true; schedule(); });
    box.addEventListener('mouseleave', function () { hold = false; schedule(); });
    render();
    schedule();
    return {
      update: update,
      tick: tick,
      run: function (on) { running = on; schedule(); }
    };
  })();

  // One poller for the page. It asks only while someone is looking: never
  // while the tab is hidden or the browser offline, and not after 10 minutes
  // with no mouse, key, touch or scroll. Coming back costs one request, and
  // only if the figures are over a minute old.
  (function () {
    var url = root.getAttribute('data-live-url');
    if (!url || !live) return;
    var MINUTE = 60000, IDLE = 10 * MINUTE, BACKOFF = [MINUTE, 2 * MINUTE, 5 * MINUTE];
    var last = Date.now(), lastActive = Date.now();
    var timer = null, inflight = null, fails = 0, stopped = false, wasActive = true;
    var note = root.querySelector('[data-bd-updated]');
    var dot = root.querySelector('.bd-live-dot');

    function hhmm(t) {
      return new Date(t).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
    }
    function active() {
      return !stopped && document.visibilityState === 'visible' && navigator.onLine !== false
        && Date.now() - lastActive < IDLE;
    }
    function label() {
      var on = active();
      if (note) note.textContent = stopped ? 'Signed out · updated ' + hhmm(last)
        : (on ? 'Live · updated ' : 'Paused · updated ') + hhmm(last);
      if (dot) {
        dot.classList.toggle('is-paused', !on);
        dot.lastChild.textContent = on ? 'Live' : 'Paused';
      }
    }
    function schedule(delay) {
      clearTimeout(timer);
      if (active()) timer = setTimeout(load, Math.max(0, delay));
    }
    function load() {
      if (inflight || !active()) return;
      var ctrl = window.AbortController ? new AbortController() : null;
      inflight = ctrl || {};
      fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' }, signal: ctrl && ctrl.signal })
        .then(function (r) {
          if (r.status === 401) { stopped = true; throw new Error('signed out'); }
          if (!r.ok) throw new Error(String(r.status));
          return r.json();
        })
        .then(function (data) {
          fails = 0;
          last = Date.now();
          if (carousel) carousel.update(data.branches || []);
          if (overdue) overdue.update(data.overdue || { on_rent: 0, overdue: 0, rows: [] });
        })
        .catch(function (err) { if (!stopped && err.name !== 'AbortError') fails++; })
        .then(function () {
          inflight = null;
          label();
          schedule(fails ? BACKOFF[Math.min(fails, BACKOFF.length) - 1] : MINUTE);
        });
    }
    // Visibility, network or activity changed: pause or resume everything.
    function wake() {
      var on = active();
      if (on !== wasActive) {
        wasActive = on;
        if (carousel) carousel.run(on);
        if (overdue) overdue.run(on);
      }
      label();
      if (!on) {
        clearTimeout(timer);
        if (inflight && inflight.abort) inflight.abort();
        return;
      }
      if (overdue) overdue.tick();
      if (Date.now() - last >= MINUTE) load();
      else if (!inflight) schedule(MINUTE - (Date.now() - last));
    }
    function touched() {
      var idle = Date.now() - lastActive >= IDLE;
      lastActive = Date.now();
      if (idle) wake();
    }

    ['mousemove', 'pointerdown', 'keydown', 'wheel', 'touchstart', 'scroll'].forEach(function (name) {
      window.addEventListener(name, touched, { passive: true });
    });
    document.addEventListener('visibilitychange', wake);
    window.addEventListener('online', wake);
    window.addEventListener('offline', wake);
    // Housekeeping every 30 s: the overdue minutes, and noticing the user went idle.
    setInterval(function () {
      if (document.visibilityState !== 'visible') return;
      if (wasActive && !active()) { wake(); return; }
      if (overdue) overdue.tick();
    }, 30000);

    label();
    schedule(MINUTE);
  })();

  /* ── segmented range control (visual state only) ─────────────── */
  root.querySelectorAll('.bd-segment button').forEach(function (b) {
    b.addEventListener('click', function () {
      b.parentNode.querySelectorAll('button').forEach(function (o) { o.classList.remove('is-on'); });
      b.classList.add('is-on');
    });
  });

  /* ── monthly sales sparkline ─────────────────────────────────── */
  // One point per day of the month so far, quiet days included. The peak
  // and best-day labels come from the server.
  (function () {
    var svg = root.querySelector('#bd-spark');
    var series = (data.daily_revenue || []).slice();
    if (!svg || !series.length) return;
    if (series.length === 1) series.push(series[0]);
    var lo = Math.min.apply(null, series), hi = Math.max.apply(null, series);
    if (hi <= lo) hi = lo + 1;                       // a flat month draws a flat line, not NaN
    var pts = series.map(function (v, i) {
      return [(i / (series.length - 1)) * 296 + 2, 92 - ((v - lo) / (hi - lo)) * 78];
    });
    var line = pts.map(function (p, i) { return (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1); }).join(' ');

    var defs = el('defs');
    var grad = el('linearGradient', { id: 'bdSparkFill', x1: '0', y1: '0', x2: '0', y2: '1' });
    grad.appendChild(el('stop', { offset: '0%', 'stop-color': RED, 'stop-opacity': '.24' }));
    grad.appendChild(el('stop', { offset: '100%', 'stop-color': RED, 'stop-opacity': '0' }));
    defs.appendChild(grad);
    svg.appendChild(defs);
    svg.appendChild(el('path', { d: line + ' L298 96 L2 96 Z', fill: 'url(#bdSparkFill)' }));
    svg.appendChild(el('path', {
      d: line, fill: 'none', stroke: RED, 'stroke-width': '1.8',
      'stroke-linecap': 'round', 'stroke-linejoin': 'round'
    }));

    var peakIdx = 0;
    series.forEach(function (v, i) { if (v > series[peakIdx]) peakIdx = i; });
    if (series[peakIdx] <= 0) return;
    var px = pts[peakIdx][0].toFixed(1), py = pts[peakIdx][1].toFixed(1);
    var g = el('g', { class: 'bd-spark-marker' });
    g.appendChild(el('line', { x1: px, y1: '0', x2: px, y2: '96', stroke: '#1a1640', 'stroke-width': '.8', 'stroke-dasharray': '3 3', opacity: '.28' }));
    g.appendChild(el('circle', { cx: px, cy: py, r: '3.6', fill: RED, stroke: '#fff', 'stroke-width': '2' }));
    svg.appendChild(g);
  })();

  /* ── revenue reports: 12 month bars ──────────────────────────── */
  (function () {
    var host = root.querySelector('#bd-months');
    var vals = data.monthly_revenue || [], labels = data.monthly_labels || [];
    if (!host || !vals.length) return;
    var max = Math.max.apply(null, vals), min = Math.min.apply(null, vals);
    vals.forEach(function (v, i) {
      var wrap = document.createElement('div');
      wrap.className = 'bd-bar';
      wrap.title = labels[i] + ' · AED ' + fmt(v);
      var bar = document.createElement('i');
      bar.style.height = Math.round((v / max) * 112) + 'px';
      bar.style.background = v >= max * 0.93 ? RED : (v <= min * 1.07 ? PALE : MID);
      var lab = document.createElement('span');
      lab.textContent = labels[i];
      wrap.appendChild(bar);
      wrap.appendChild(lab);
      host.appendChild(wrap);
    });
    var pi = vals.indexOf(max), ti = vals.indexOf(min);
    var peak = root.querySelector('#bd-month-peak'), trough = root.querySelector('#bd-month-trough');
    if (peak) peak.textContent = labels[pi] + ' peak · ' + fmt(max);
    if (trough) trough.textContent = labels[ti] + ' trough · ' + fmt(min);
  })();

  /* ── revenue by category ─────────────────────────────────────── */
  (function () {
    var host = root.querySelector('#bd-categories');
    var names = data.revenue_categories || [], vals = data.revenue_category_values || [];
    if (!host || !names.length) return;
    var max = Math.max.apply(null, vals);
    names.forEach(function (name, i) {
      var row = document.createElement('div');
      row.className = 'bd-catrow';
      row.innerHTML = '<span class="name"></span><span class="bar"><i></i></span><span class="val"></span>';
      row.querySelector('.name').textContent = name;
      row.querySelector('.name').title = name;
      row.querySelector('.val').textContent = fmt(vals[i]);
      var bar = row.querySelector('.bar i');
      bar.style.width = Math.max(1.2, (vals[i] / max) * 100) + '%';
      bar.style.background = i === 0 ? RED : (i < 4 ? MID : PALE);
      host.appendChild(row);
    });
  })();

  /* ── deployment gauge: 42 ticks over a 270° sweep ────────────── */
  (function () {
    var svg = root.querySelector('#bd-gauge');
    if (!svg) return;
    var pct = (Number(svg.dataset.pct) || 0) / 100;
    var TICKS = 42;
    for (var i = 0; i < TICKS; i++) {
      var a = ((135 + (i / (TICKS - 1)) * 270) * Math.PI) / 180;
      var f = i / (TICKS - 1);
      svg.appendChild(el('line', {
        x1: (98 + Math.cos(a) * 68).toFixed(1), y1: (98 + Math.sin(a) * 68).toFixed(1),
        x2: (98 + Math.cos(a) * 86).toFixed(1), y2: (98 + Math.sin(a) * 86).toFixed(1),
        stroke: f <= pct ? (f > 0.7 ? RED : '#e8474d') : '#f2dcdc',
        'stroke-width': '5', 'stroke-linecap': 'round'
      }));
    }
  })();

  /* Station Network is a real Leaflet tile map (byky-leaflet-map.js), not
     drawn here -- placing a station needs actual geography, which a bubble
     plot on a blank grid cannot show. data.map_points still feeds it, via
     the map_points json_script node in the template. */
})();

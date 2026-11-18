/**
 * Station Network -- the dashboard's map widget.
 *
 * Opens on one station at street zoom (the busiest, or the one this viewer
 * chose last), with a summary of its live figures beside the map. The arrows,
 * the station list and the pins all choose a station; the map then moves
 * behind a blurred veil with a small spinner and is revealed already framed
 * on the new station, so the viewer never watches tiles load. With reduced
 * motion the map jumps and the veil only fades.
 *
 * Every station has the same pin. Pins that would overlap at a wide zoom
 * merge into one quiet group marker; at street zoom each pin carries its
 * station's name.
 *
 * Data: DashboardView.map_points (#bd-map-points). Figures refresh from the
 * dashboard's own poller (byky-dashboard.js fires `bd:live` with the
 * /dashboard/live/ payload); only text changes, so the view, the selection
 * and any open tooltip stay as they are.
 */

'use strict';

(function () {
  var ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services';
  var ESRI_ATTR = 'Tiles &copy; Esri &mdash; &copy; OpenStreetMap contributors';
  var STREET_ZOOM = 15;     // one station area with the roads around it
  var LABEL_ZOOM = 13;      // pins carry their names from here in
  var VEIL_IN = 120;        // ms: veil fades in before the map jumps
  var TILE_WAIT = [160, 800];  // ms: hold the veil at least / at most this long for tiles
  var STORE = 'bsn-station';

  var PIN = '<svg class="stn-pin" viewBox="0 0 28 36" width="28" height="36" aria-hidden="true">' +
    '<path class="stn-body" d="M14 1.2C7.2 1.2 1.7 6.6 1.7 13.3c0 8.6 10.1 18.9 11.3 20.1a1.4 1.4 0 0 0 2 0c1.2-1.2 11.3-11.5 11.3-20.1C26.3 6.6 20.8 1.2 14 1.2z"/>' +
    '<g class="stn-bike" fill="none" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' +
    '<circle cx="9" cy="15.6" r="2.9"/><circle cx="19" cy="15.6" r="2.9"/>' +
    '<path d="M9 15.6l3-5.4h4.4M12 10.2l2.6 5.4L19 15.6l-2.6-5.4M11.2 8.4h2.2"/></g></svg>';
  var MINI_PIN = '<svg viewBox="0 0 28 36" width="10" height="13" aria-hidden="true">' +
    '<path d="M14 1.2C7.2 1.2 1.7 6.6 1.7 13.3c0 8.6 10.1 18.9 11.3 20.1a1.4 1.4 0 0 0 2 0c1.2-1.2 11.3-11.5 11.3-20.1C26.3 6.6 20.8 1.2 14 1.2z"/></svg>';

  function baseLayers() {
    // Light grey is drawn natively to z16; beyond that its tiles are scaled.
    return {
      light: L.layerGroup([
        L.tileLayer(ESRI + '/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}', { attribution: ESRI_ATTR, maxNativeZoom: 16, maxZoom: 18 }),
        L.tileLayer(ESRI + '/Canvas/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}', { maxNativeZoom: 16, maxZoom: 18 })
      ]),
      streets: L.tileLayer(ESRI + '/World_Street_Map/MapServer/tile/{z}/{y}/{x}', {
        attribution: ESRI_ATTR, maxZoom: 18
      }),
      satellite: L.layerGroup([
        L.tileLayer(ESRI + '/World_Imagery/MapServer/tile/{z}/{y}/{x}', { attribution: ESRI_ATTR, maxZoom: 18 }),
        L.tileLayer(ESRI + '/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}', { maxZoom: 18 })
      ])
    };
  }

  function tilesOf(layer) {
    if (layer instanceof L.TileLayer) return [layer];
    var out = [];
    if (layer && layer.eachLayer) layer.eachLayer(function (l) { if (l instanceof L.TileLayer) out.push(l); });
    return out;
  }

  function esc(t) { var d = document.createElement('div'); d.textContent = t == null ? '' : String(t); return d.innerHTML; }
  function num(v) { return Number(String(v == null ? 0 : v).replace(/,/g, '')) || 0; }
  function fmt(n) { return Number(n || 0).toLocaleString('en-US'); }
  function plural(n, one, many) { return n === 1 ? one : many; }
  function reduced() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }
  function store(k, v) {
    try { if (v === undefined) return window.localStorage.getItem(k); window.localStorage.setItem(k, v); } catch (e) { /* private window */ }
    return null;
  }

  function build(root) {
    var src = document.getElementById(root.dataset.points);
    if (!src) return;
    var S = JSON.parse(src.textContent) || [];
    var el = root.querySelector('.bsn-map');
    if (!S.length || !el) return;

    var wrap = root.querySelector('.bsn-mapwrap');
    var layers = baseLayers();
    var base = layers.light;
    // On touch screens one finger scrolls the page, not the map: pan with
    // the zoom buttons, the chips and the list, or pinch.
    var map = L.map(el, { zoomControl: true, scrollWheelZoom: false, dragging: !L.Browser.mobile, maxZoom: 18, layers: [base] });
    L.control.layers(
      { 'Light grey': layers.light, 'Streets': layers.streets, 'Satellite': layers.satellite },
      null, { position: 'topright' }
    ).addTo(map);
    map.on('baselayerchange', function (e) { base = e.layer; });

    // "Every station" sits under the zoom buttons: the way back out.
    var Overview = L.Control.extend({
      options: { position: 'topleft' },
      onAdd: function () {
        var bar = L.DomUtil.create('div', 'leaflet-bar bsn-overview');
        var a = L.DomUtil.create('a', '', bar);
        a.href = '#';
        a.setAttribute('role', 'button');
        a.title = 'Show every station';
        a.setAttribute('aria-label', 'Show every station');
        a.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.4 2.4 3.4 5.3 3.4 8.5s-1 6.1-3.4 8.5M12 3.5C9.6 5.9 8.6 8.8 8.6 12s1 6.1 3.4 8.5"/></g></svg>';
        L.DomEvent.disableClickPropagation(bar);
        L.DomEvent.on(a, 'click', function (e) { L.DomEvent.preventDefault(e); setFilter(null); });
        L.DomEvent.on(a, 'keydown', function (e) {
          if (e.key === ' ') { L.DomEvent.preventDefault(e); setFilter(null); }
        });
        return bar;
      }
    });
    new Overview().addTo(map);

    var byId = {};
    var groups = {};
    S.forEach(function (s, i) {
      byId[s.id] = i;
      (groups[s.emirate] = groups[s.emirate] || { list: [] }).list.push(i);
    });
    var emirates = Object.keys(groups).sort(function (a, b) {
      return groups[b].list.length - groups[a].list.length || a.localeCompare(b);
    });

    // Kuwait is plotted but kept out of the overview frame -- one station
    // there otherwise pulls the view out to the whole Gulf.
    var uae = S.filter(function (s) { return s.emirate !== 'Kuwait'; });
    var allBounds = L.latLngBounds((uae.length ? uae : S).map(function (s) { return [s.lat, s.lng]; })).pad(0.12);
    function boundsOf(idx) {
      return L.latLngBounds(idx.map(function (i) { return [S[i].lat, S[i].lng]; })).pad(0.3);
    }

    var layer = L.layerGroup().addTo(map);
    var markers = {};
    var filter = null;
    var sel = null;
    var lit = null;
    var shown = [];          // indices in the current filter, busiest first

    /* ── ordering: busiest now, then today's takings, then fleet ───── */
    function rankOrder() {
      shown = S.map(function (s, i) { return i; })
        .filter(function (i) { return !filter || S[i].emirate === filter; })
        .sort(function (a, b) {
          var x = S[a], y = S[b];
          return num(y.on_rent) - num(x.on_rent) || num(y.revenue) - num(x.revenue) ||
            num(y.fleet) - num(x.fleet) || x.name.localeCompare(y.name);
        });
    }

    /* ── pins and quiet groups ─────────────────────────────────────── */
    function tip(i) {
      var s = S[i];
      return '<div class="t">' + esc(s.name) + '</div><div class="l">' + esc(s.emirate) + '</div>' +
        '<div class="stats"><span class="stat"><u>Fleet</u><b>' + fmt(s.fleet) + '</b></span>' +
        '<span class="stat"><u>On rent</u><b>' + fmt(s.on_rent) + '</b></span></div>';
    }
    function groupTip(idx) {
      var ems = {};
      idx.forEach(function (i) { ems[S[i].emirate] = 1; });
      var names = Object.keys(ems).map(esc);
      return '<div class="t">' + idx.length + ' stations</div><div class="l">' + names.join(' · ') + '</div>' +
        '<div class="note">Click to zoom in</div>';
    }

    function pinItem(idx) {
      var x = 0, y = 0;
      idx.forEach(function (i) {
        var p = map.latLngToContainerPoint([S[i].lat, S[i].lng]);
        x += p.x; y += p.y;
      });
      x /= idx.length; y /= idx.length;
      // A pin stands on its point, so its drawn centre is ~17px above it; a
      // group pill is centred on its point. Collisions use the drawn centre.
      var solo = idx.length === 1;
      return { idx: idx, x: x, y: y, cx: x, cy: solo ? y - 17 : y, r: solo ? 15 : 22 };
    }
    // Merge any two marks that would touch; a merged group sits at its
    // members' centroid, so keep merging until nothing overlaps.
    function layout() {
      var items = [];
      S.forEach(function (s, i) {
        if (filter && s.emirate !== filter) return;
        if (i !== sel) items.push(pinItem([i]));
      });
      // The chosen station always keeps its own pin and label, and at street
      // zoom nothing merges: every station there is a pin you can pick.
      var chosen = sel != null && (!filter || S[sel].emirate === filter) ? pinItem([sel]) : null;
      var moved = map.getZoom() < STREET_ZOOM, guard = 0;
      while (moved && guard++ < 200) {
        moved = false;
        for (var a = 0; a < items.length && !moved; a++) {
          for (var b = a + 1; b < items.length; b++) {
            var dx = items[a].cx - items[b].cx, dy = items[a].cy - items[b].cy;
            var need = items[a].r + items[b].r + 2;
            if (dx * dx + dy * dy < need * need) {
              items[a] = pinItem(items[a].idx.concat(items[b].idx));
              items.splice(b, 1);
              moved = true;
              break;
            }
          }
        }
      }
      if (chosen) items.push(chosen);
      return items;
    }

    function pin(i) {
      var s = S[i];
      var m = L.marker([s.lat, s.lng], {
        icon: L.divIcon({
          className: 'bsn-mark',
          html: '<div class="stn' + (i === sel ? ' sel' : '') + (i === lit ? ' lit' : '') + '" data-i="' + i + '" aria-hidden="true">' +
            PIN + '<span class="stn-label">' + esc(s.name) + '</span></div>',
          iconSize: [28, 36], iconAnchor: [14, 35]
        }),
        keyboard: false,            // the station list is the keyboard path
        zIndexOffset: i === sel ? 1000 : 0
      });
      m.bindTooltip(function () { return tip(i); }, { className: 'bsn-tip', direction: 'top', offset: [0, -34], opacity: 1 });
      m.on('mouseover', function () { setLit(i); });
      m.on('mouseout', function () { setLit(null); });
      m.on('click', function () { choose(i, true); });
      markers[i] = m;
      return m;
    }

    function group(idx, centre) {
      var m = L.marker(centre, {
        icon: L.divIcon({
          className: 'bsn-mark',
          html: '<div class="grp" aria-hidden="true">' + MINI_PIN + '<b>' + idx.length + '</b></div>',
          iconSize: [44, 26], iconAnchor: [22, 13]
        }),
        keyboard: false
      });
      m.bindTooltip(groupTip(idx), { className: 'bsn-tip', direction: 'top', offset: [0, -14], opacity: 1 });
      m.on('click', function () { travel(function () { map.fitBounds(boundsOf(idx), { animate: false, maxZoom: STREET_ZOOM }); }); });
      idx.forEach(function (i) { markers[i] = null; });
      return m;
    }

    function draw() {
      layer.clearLayers();
      markers = {};
      layout().forEach(function (it) {
        if (it.idx.length === 1) pin(it.idx[0]).addTo(layer);
        else group(it.idx, map.containerPointToLatLng([it.x, it.y])).addTo(layer);
      });
      el.classList.toggle('show-labels', map.getZoom() >= LABEL_ZOOM);
    }
    map.on('zoomend moveend', draw);

    function setLit(i) {
      lit = i;
      root.querySelectorAll('.stn').forEach(function (n) { n.classList.toggle('lit', Number(n.dataset.i) === i); });
      root.querySelectorAll('.bsn-row').forEach(function (n) { n.classList.toggle('lit', Number(n.dataset.i) === i); });
    }

    // Street zoom on station i, centring the pin and its name label together
    // (the label hangs to the right), so a narrow map never cuts the name.
    function frame(i) {
      var shift = Math.min(80, el.clientWidth / 4);
      var pt = map.project([S[i].lat, S[i].lng], STREET_ZOOM).add([shift, -18]);
      map.setView(map.unproject(pt, STREET_ZOOM), STREET_ZOOM, { animate: false });
    }

    /* ── the veil: mask the move, reveal the new frame ──────────────── */
    var trip = 0;
    function waitTiles(done, cap) {
      var started = Date.now();
      (function check() {
        var waited = Date.now() - started;
        var busy = tilesOf(base).some(function (t) { return t.isLoading && t.isLoading(); });
        if ((waited >= TILE_WAIT[0] && !busy) || waited >= (cap || TILE_WAIT[1])) done();
        else setTimeout(check, 40);
      })();
    }
    function travel(apply) {
      var mine = ++trip;
      if (reduced()) {
        wrap.classList.add('is-cut');
        apply();
        waitTiles(function () { if (mine === trip) wrap.classList.remove('is-cut'); });
        return;
      }
      wrap.classList.add('is-moving');
      setTimeout(function () {
        if (mine !== trip) return;
        apply();
        waitTiles(function () { if (mine === trip) wrap.classList.remove('is-moving'); });
      }, VEIL_IN);
    }

    /* ── the summary ────────────────────────────────────────────────── */
    var sum = root.querySelector('.bsn-sum');
    var say = root.querySelector('[data-announce]');
    function q(sel_) { return sum.querySelector(sel_); }

    function fillFigures() {
      if (sel == null) return;
      var s = S[sel];
      q('[data-f="revenue"]').textContent = s.revenue == null ? '—' : s.revenue;
      var inv = num(s.invoices);
      q('[data-f="invoices"]').textContent = fmt(inv);
      q('[data-f="invoices-word"]').textContent = plural(inv, 'invoice', 'invoices');
      ['on_rent', 'fleet', 'devices', 'staff'].forEach(function (f) {
        q('[data-f="' + f + '"]').textContent = s[f] == null ? '—' : fmt(s[f]);
      });
      var state = q('[data-s="state"]');
      state.hidden = !(s.state === 'open' || s.state === 'closed');
      state.className = 'bsn-state is-' + s.state;
      state.textContent = s.state === 'open' ? 'Open' : 'Closed';
      q('[data-s="hours"]').textContent = s.hours || '';
    }

    function fillSummary() {
      var s = S[sel];
      q('[data-s="name"]').textContent = s.name;
      q('[data-s="emirate"]').textContent = s.emirate;
      var k = shown.indexOf(sel);
      q('[data-s="pos"]').textContent = 'Station ' + (k + 1) + ' of ' + shown.length;
      var lone = shown.length < 2;
      sum.querySelectorAll('[data-step]').forEach(function (b) { b.disabled = lone; });
      var orders = q('[data-s="orders"]');
      orders.href = root.dataset.ordersUrl + '?branch=' + encodeURIComponent(s.id);
      q('[data-s="orders-at"]').textContent = ' at ' + s.name;
      fillFigures();
    }

    function announce() {
      var s = S[sel];
      say.textContent = s.name + ', ' + s.emirate + '. Station ' + (shown.indexOf(sel) + 1) + ' of ' + shown.length +
        '. ' + fmt(s.on_rent) + ' on rent of ' + fmt(s.fleet) + '. Revenue today AED ' + (s.revenue || '0') + '.';
    }

    function markSelected() {
      var rows = root.querySelectorAll('.bsn-row');
      var hasSel = false;
      rows.forEach(function (n) {
        var on = Number(n.dataset.i) === sel;
        hasSel = hasSel || on;
        n.classList.toggle('sel', on);
        n.setAttribute('aria-selected', on ? 'true' : 'false');
        if (n !== document.activeElement) n.tabIndex = on ? 0 : -1;
      });
      if (!hasSel && rows.length) rows[0].tabIndex = 0;
    }

    function scrollRowIntoView() {
      var row = root.querySelector('.bsn-row.sel');
      var box = root.querySelector('.bsn-rows');
      if (!row || !box) return;
      var top = row.offsetTop, bottom = top + row.offsetHeight;
      if (top < box.scrollTop) box.scrollTop = top;
      else if (bottom > box.scrollTop + box.clientHeight) box.scrollTop = bottom - box.clientHeight;
    }

    // Choose station i; `fly` moves the map there at street zoom.
    function choose(i, fly, quiet, fromList) {
      sel = i;
      if (fromList && window.matchMedia && window.matchMedia('(max-width: 720px)').matches) {
        wrap.scrollIntoView({ block: 'start', behavior: reduced() ? 'auto' : 'smooth' });
      }
      store(STORE, String(S[i].id));
      fillSummary();
      markSelected();
      scrollRowIntoView();
      if (!quiet) announce();
      if (fly) travel(function () { frame(i); draw(); });
      else draw();
    }

    sum.querySelectorAll('[data-step]').forEach(function (b) {
      b.addEventListener('click', function () {
        if (shown.length < 2) return;
        var k = shown.indexOf(sel);
        k = (k < 0 ? 0 : k + Number(b.dataset.step) + shown.length) % shown.length;
        choose(shown[k], true);
      });
    });

    /* ── station list ──────────────────────────────────────────────── */
    function rowFigure(s) {
      return '<b>' + fmt(s.fleet) + '</b><s>vehicles</s>';
    }
    function rowMeta(s) {
      return s.emirate + (num(s.on_rent) ? ' · ' + fmt(s.on_rent) + ' on rent' : '');
    }
    function drawList() {
      root.querySelector('[data-list-title]').textContent = filter || 'All stations';
      var total = shown.reduce(function (a, i) { return a + num(S[i].fleet); }, 0);
      root.querySelector('[data-list-meta]').textContent =
        shown.length + ' ' + plural(shown.length, 'station', 'stations') + ' · ' + fmt(total) + ' vehicles';

      var rows = root.querySelector('.bsn-rows');
      rows.innerHTML = '';
      shown.forEach(function (i, k) {
        var s = S[i];
        var b = document.createElement('div');
        b.className = 'bsn-row';
        b.setAttribute('role', 'option');
        b.id = 'bsn-row-' + S[i].id;
        b.tabIndex = -1;
        b.dataset.i = i;
        b.innerHTML = '<span class="rank">' + (k + 1) + '</span>' +
          '<span class="body"><span class="n"></span><span class="p"></span></span>' +
          '<span class="fig">' + rowFigure(s) + '</span>';
        b.querySelector('.n').textContent = s.name;
        b.querySelector('.p').textContent = rowMeta(s);
        b.addEventListener('mouseenter', function () { setLit(i); });
        b.addEventListener('mouseleave', function () { setLit(null); });
        b.addEventListener('focus', function () { setLit(i); });
        b.addEventListener('blur', function () { setLit(null); });
        b.addEventListener('click', function () { choose(i, true, false, true); });
        rows.appendChild(b);
      });
      markSelected();
    }

    // One tab stop for the whole list: arrows, Home and End move, Enter or
    // Space chooses (the listbox pattern).
    root.querySelector('.bsn-rows').addEventListener('keydown', function (e) {
      var all = Array.prototype.slice.call(root.querySelectorAll('.bsn-row'));
      var at = all.indexOf(document.activeElement);
      if (at < 0) return;
      var to = { ArrowDown: at + 1, ArrowUp: at - 1, Home: 0, End: all.length - 1 }[e.key];
      if (to != null) {
        e.preventDefault();
        to = Math.max(0, Math.min(all.length - 1, to));
        all.forEach(function (r, k) { r.tabIndex = k === to ? 0 : -1; });
        all[to].focus();
        all[to].scrollIntoView({ block: 'nearest' });
      } else if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        choose(Number(all[at].dataset.i), true);
      }
    });

    /* ── emirate chips ──────────────────────────────────────────────── */
    function drawChips() {
      var host = root.querySelector('.bsn-chips');
      host.innerHTML = '';
      function chip(label, value, count, on) {
        var c = document.createElement('button');
        c.type = 'button';
        c.className = 'bsn-chip' + (on ? ' on' : '');
        c.textContent = label;
        c.setAttribute('aria-pressed', on ? 'true' : 'false');
        var n = document.createElement('b');
        n.textContent = count;
        c.appendChild(n);
        c.setAttribute('aria-label', label + ', ' + count + ' ' + plural(count, 'station', 'stations'));
        c.addEventListener('click', function () { setFilter(value); });
        host.appendChild(c);
      }
      chip('All emirates', null, S.length, !filter);
      emirates.forEach(function (name) { chip(name, name, groups[name].list.length, filter === name); });
    }

    function syncSub() {
      var sub = root.querySelector('[data-sub]');
      if (!sub) return;
      var n = shown.length;
      sub.textContent = n + ' ' + plural(n, 'station', 'stations') + ' · ' +
        (filter || emirates.length + ' ' + plural(emirates.length, 'emirate', 'emirates'));
    }

    // An emirate (or every station) framed on the map; the summary keeps its
    // station if it is in the filter, or takes the filter's busiest.
    function setFilter(name) {
      filter = name;
      rankOrder();
      drawChips();
      drawList();
      syncSub();
      if (sel == null || shown.indexOf(sel) < 0) choose(shown[0], false, true);
      else { fillSummary(); markSelected(); }
      scrollRowIntoView();
      say.textContent = (name || 'All emirates') + ': ' + shown.length + ' ' + plural(shown.length, 'station', 'stations') +
        '. Showing ' + S[sel].name + '.';
      travel(function () {
        map.fitBounds(name ? boundsOf(groups[name].list) : allBounds, { animate: false, maxZoom: STREET_ZOOM });
      });
    }

    /* ── live figures: text only, nothing redrawn or reordered ─────── */
    document.addEventListener('bd:live', function (e) {
      var rows = (e.detail && e.detail.branches) || [];
      rows.forEach(function (r) {
        var i = byId[r.id];
        if (i == null) return;
        ['fleet', 'on_rent', 'invoices', 'revenue', 'devices', 'staff', 'hours', 'state'].forEach(function (f) {
          if (f in r) S[i][f] = r[f];
        });
      });
      fillFigures();
      root.querySelectorAll('.bsn-row').forEach(function (b) {
        b.querySelector('.fig').innerHTML = rowFigure(S[Number(b.dataset.i)]);
        b.querySelector('.p').textContent = rowMeta(S[Number(b.dataset.i)]);
      });
      var total = shown.reduce(function (a, i) { return a + num(S[i].fleet); }, 0);
      root.querySelector('[data-list-meta]').textContent =
        shown.length + ' ' + plural(shown.length, 'station', 'stations') + ' · ' + fmt(total) + ' vehicles';
    });

    /* ── first frame: one station at street zoom ───────────────────── */
    rankOrder();
    var saved = byId[store(STORE)];
    sel = saved != null ? saved : shown[0];
    drawChips();
    drawList();
    syncSub();
    fillSummary();
    var first = ++trip;
    wrap.classList.add('is-moving');
    frame(sel);
    setTimeout(function () {
      map.invalidateSize();
      if (first !== trip) return;          // the viewer already moved the map
      frame(sel);
      waitTiles(function () { if (first === trip) wrap.classList.remove('is-moving'); }, 1500);
    }, 120);
    scrollRowIntoView();
  }

  function init() {
    if (typeof L === 'undefined') return;
    document.querySelectorAll('.bsn[data-points]').forEach(build);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

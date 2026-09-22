/**
 * Station Network -- the dashboard's map widget.
 *
 * Ported from the approved "Byky Station Network.html": its clustering,
 * pins, cluster bubbles, tooltips, size legend, emirate chips and station
 * list, kept value-for-value. Two things come from this codebase instead of
 * the reference:
 *
 *   tiles   the site's own Esri basemaps and layer switcher, the same set
 *           byky-leaflet-map.js uses everywhere else -- OSM's raw tiles
 *           render UAE place names in Arabic, and this UI is English.
 *   data    real stations from geo.py, with indicative revenue and
 *           on-rent counts from sales.py.
 *
 * Two deliberate departures from the reference, both requested:
 *   - the right-hand list carries fleet, not revenue; revenue is on the pin.
 *   - hovering a row only highlights it. The map moves, and the station's
 *     details open, on click. The reference flew the map on hover, which
 *     made scanning the list feel like the map was fighting you.
 */

'use strict';

(function () {
  var ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services';
  var ESRI_ATTR = 'Tiles &copy; Esri &mdash; &copy; OpenStreetMap contributors';
  var CLUSTER_CELL = 62;

  function baseLayers() {
    return {
      streets: L.tileLayer(ESRI + '/World_Street_Map/MapServer/tile/{z}/{y}/{x}', {
        attribution: ESRI_ATTR, maxZoom: 18
      }),
      light: L.layerGroup([
        L.tileLayer(ESRI + '/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}', { attribution: ESRI_ATTR, maxZoom: 16 }),
        L.tileLayer(ESRI + '/Canvas/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}', { maxZoom: 16 })
      ]),
      satellite: L.layerGroup([
        L.tileLayer(ESRI + '/World_Imagery/MapServer/tile/{z}/{y}/{x}', { attribution: ESRI_ATTR, maxZoom: 18 }),
        L.tileLayer(ESRI + '/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}', { maxZoom: 18 })
      ])
    };
  }

  function build(root) {
    var src = document.getElementById(root.dataset.points);
    if (!src) return;
    var S = JSON.parse(src.textContent) || [];
    if (!S.length) return;

    var fmt = function (n) { return Number(n || 0).toLocaleString('en-US'); };
    var el = root.querySelector('.bsn-map');
    var layers = baseLayers();

    var map = L.map(el, { zoomControl: true, scrollWheelZoom: false, layers: [layers.streets] });
    L.control.layers(
      { 'Streets': layers.streets, 'Light': layers.light, 'Satellite': layers.satellite },
      null, { position: 'topright' }
    ).addTo(map);

    /* Kuwait is plotted but kept out of the opening frame -- one station
       there otherwise pulls the view out to the whole Gulf. */
    var uae = S.filter(function (s) { return s.emirate !== 'Kuwait'; });
    var bounds = L.latLngBounds((uae.length ? uae : S).map(function (s) { return [s.lat, s.lng]; })).pad(0.16);
    map.fitBounds(bounds);

    var groups = {};
    S.forEach(function (s, i) {
      (groups[s.emirate] = groups[s.emirate] || { list: [], fleet: 0 });
      groups[s.emirate].list.push(i);
      groups[s.emirate].fleet += s.fleet || 0;
    });
    var emirates = Object.keys(groups).sort(function (a, b) { return groups[b].fleet - groups[a].fleet; });

    var layer = L.layerGroup().addTo(map);
    var markers = {};
    var clusterOf = {};
    var filter = null;
    var lit = null;

    function radius(fleet) { return Math.round(7 + Math.sqrt(fleet || 0) * 1.35); }
    function bubD(n) { return Math.round(30 + Math.min(n, 12) * 2); }

    function stationTip(i) {
      var s = S[i];
      var share = groups[s.emirate].fleet ? Math.round((s.fleet / groups[s.emirate].fleet) * 100) : 0;
      return '<div class="t">' + s.name + '</div>' +
        '<div class="l">' + s.emirate + '</div>' +
        '<div class="stats">' +
          '<span class="stat"><u>Fleet</u><b>' + (s.fleet || 0) + '<s>vehicles</s></b></span>' +
          '<span class="stat"><u>On rent</u><b>' + (s.on_rent || 0) + '<s>now</s></b></span>' +
          '<span class="stat"><u>Revenue</u><b>' + fmt(s.revenue) + '<s>AED</s></b></span>' +
        '</div>' +
        '<div class="note">' + share + '% of ' + s.emirate + ' fleet · revenue and on-rent are indicative</div>';
    }

    function clusterTip(idx) {
      var fleet = 0, rev = 0, rent = 0, ems = {};
      idx.forEach(function (i) {
        fleet += S[i].fleet || 0; rev += S[i].revenue || 0; rent += S[i].on_rent || 0;
        ems[S[i].emirate] = 1;
      });
      var names = Object.keys(ems);
      return '<div class="t">' + (names.length === 1 ? names[0] : names.length + ' emirates') + '</div>' +
        '<div class="l">' + (names.length === 1 ? idx.length + ' stations' : names.join(' · ')) + '</div>' +
        '<div class="stats">' +
          '<span class="stat"><u>Fleet</u><b>' + fleet + '<s>vehicles</s></b></span>' +
          '<span class="stat"><u>On rent</u><b>' + rent + '<s>now</s></b></span>' +
          '<span class="stat"><u>Revenue</u><b>' + fmt(rev) + '<s>AED</s></b></span>' +
        '</div>' +
        '<div class="note">Click to expand</div>';
    }

    /* Bucket into grid cells, then merge any two groups whose drawn circles
       would touch: each bubble sits at its members' centroid, so neighbouring
       cells can still yield centres closer than one cell. */
    function makeItem(idx) {
      var x = 0, y = 0;
      idx.forEach(function (i) {
        var p = map.latLngToContainerPoint([S[i].lat, S[i].lng]);
        x += p.x; y += p.y;
      });
      x /= idx.length; y /= idx.length;
      var r = idx.length === 1 ? radius(S[idx[0]].fleet) / 2 + 4 : bubD(idx.length) / 2 + 7;
      return { idx: idx, x: x, y: y, r: r };
    }

    function layout() {
      var items = [], cells = {};
      S.forEach(function (s, i) {
        if (filter && s.emirate !== filter) return;
        var p = map.latLngToContainerPoint([s.lat, s.lng]);
        var key = Math.floor(p.x / CLUSTER_CELL) + ':' + Math.floor(p.y / CLUSTER_CELL);
        (cells[key] = cells[key] || []).push(i);
      });
      Object.keys(cells).forEach(function (k) { items.push(makeItem(cells[k])); });

      var moved = true, guard = 0;
      while (moved && guard++ < 60) {
        moved = false;
        for (var a = 0; a < items.length && !moved; a++) {
          for (var b = a + 1; b < items.length; b++) {
            var dx = items[a].x - items[b].x, dy = items[a].y - items[b].y;
            var need = items[a].r + items[b].r + 8;
            if (dx * dx + dy * dy < need * need) {
              items[a] = makeItem(items[a].idx.concat(items[b].idx));
              items.splice(b, 1);
              moved = true;
              break;
            }
          }
        }
      }
      return items;
    }

    function station(i) {
      var s = S[i], d = radius(s.fleet);
      var m = L.marker([s.lat, s.lng], {
        icon: L.divIcon({
          className: '',
          html: '<div class="stn" data-i="' + i + '"><i style="width:' + d + 'px;height:' + d + 'px"></i></div>',
          iconSize: [d + 6, d + 6], iconAnchor: [(d + 6) / 2, (d + 6) / 2]
        }),
        riseOnHover: true
      });
      m.bindTooltip(stationTip(i), { className: 'bsn-tip', direction: 'top', offset: [0, -d / 2 - 6], opacity: 1 });
      m.on('mouseover', function () { setLit(i); });
      m.on('mouseout', function () { setLit(null); });
      m.on('click', function () { map.flyTo([s.lat, s.lng], 14, { duration: 0.6 }); });
      markers[i] = m;
      return m;
    }

    function cluster(idx, centre) {
      var d = bubD(idx.length);
      var m = L.marker(centre, {
        icon: L.divIcon({
          className: '',
          html: '<div class="cl-wrap"><div class="cl" style="width:' + d + 'px;height:' + d + 'px">' + idx.length + '</div></div>',
          iconSize: [d + 14, d + 14], iconAnchor: [(d + 14) / 2, (d + 14) / 2]
        })
      });
      m.bindTooltip(clusterTip(idx), { className: 'bsn-tip', direction: 'top', offset: [0, -d / 2 - 6], opacity: 1 });
      m.on('click', function () {
        map.flyToBounds(L.latLngBounds(idx.map(function (i) { return [S[i].lat, S[i].lng]; })).pad(0.4), { duration: 0.6 });
      });
      idx.forEach(function (i) { clusterOf[i] = m; });
      return m;
    }

    function draw() {
      layer.clearLayers();
      markers = {};
      clusterOf = {};
      var items = layout();
      items.forEach(function (it) {
        if (it.idx.length === 1) station(it.idx[0]).addTo(layer);
        else cluster(it.idx, map.containerPointToLatLng([it.x, it.y])).addTo(layer);
      });
      syncLegend(items.every(function (it) { return it.idx.length === 1; }));
      if (lit != null) setLit(lit);
    }
    map.on('zoomend moveend', draw);

    function syncLegend(allSolo) {
      root.querySelector('[data-scale="fleet"]').hidden = !allSolo;
      root.querySelector('[data-scale="cluster"]').hidden = allSolo;
      var n = S.filter(function (s) { return !filter || s.emirate === filter; }).length;
      var sub = root.querySelector('[data-sub]');
      if (sub) {
        sub.textContent = filter
          ? n + ' stations in ' + filter + ' — ' + (allSolo ? 'marker size reflects fleet' : 'bubbles group nearby stations')
          : n + ' stations across ' + emirates.length + ' emirates — ' +
            (allSolo ? 'marker size reflects fleet' : 'bubbles group nearby stations, sized by count');
      }
    }

    function setLit(i) {
      lit = i;
      root.querySelectorAll('.stn').forEach(function (n) {
        var idx = Number(n.dataset.i);
        n.classList.toggle('lit', idx === i);
        n.classList.toggle('dim', i != null && idx !== i);
      });
      root.querySelectorAll('.bsn-row').forEach(function (n) {
        n.classList.toggle('lit', Number(n.dataset.i) === i);
      });
    }

    /* ── station list: fleet, not revenue ───────────────────────────── */
    function drawList() {
      var shown = S.map(function (s, i) { return { s: s, i: i }; })
        .filter(function (o) { return !filter || o.s.emirate === filter; })
        .sort(function (a, b) { return (b.s.fleet || 0) - (a.s.fleet || 0); });

      root.querySelector('[data-list-title]').textContent = filter || 'All stations';
      root.querySelector('[data-list-meta]').textContent =
        fmt(shown.reduce(function (a, o) { return a + (o.s.fleet || 0); }, 0)) + ' vehicles';

      var rows = root.querySelector('.bsn-rows');
      rows.innerHTML = '';
      shown.forEach(function (o, k) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'bsn-row';
        b.dataset.i = o.i;
        b.innerHTML = '<span class="rank">' + (k + 1) + '</span>' +
          '<span class="body"><span class="n"></span><span class="p"></span></span>' +
          '<span class="fig"><b>' + (o.s.fleet || 0) + '</b><s>vehicles</s></span>';
        b.querySelector('.n').textContent = o.s.name;
        b.querySelector('.p').textContent = o.s.emirate;
        /* hover highlights only -- the map stays where the reader put it */
        b.addEventListener('mouseenter', function () { setLit(o.i); });
        b.addEventListener('mouseleave', function () { setLit(null); });
        /* click is what moves the map, and opens that station's details */
        b.addEventListener('click', function () {
          map.flyTo([o.s.lat, o.s.lng], 14, { duration: 0.6 });
          map.once('moveend', function () {
            var m = markers[o.i] || clusterOf[o.i];
            if (m) m.openTooltip();
            setLit(o.i);
          });
        });
        rows.appendChild(b);
      });
    }

    /* ── emirate chips ──────────────────────────────────────────────── */
    function drawChips() {
      var host = root.querySelector('.bsn-chips');
      host.innerHTML = '';
      function chip(label, value, count, on) {
        var c = document.createElement('button');
        c.type = 'button';
        c.className = 'bsn-chip' + (on ? ' on' : '');
        c.innerHTML = label + '<b>' + count + '</b>';
        c.onclick = function () { setFilter(value); };
        host.appendChild(c);
      }
      chip('All emirates', null, S.length, !filter);
      emirates.forEach(function (name) {
        chip(name, filter === name ? null : name, groups[name].list.length, filter === name);
      });
    }

    function setFilter(name) {
      filter = name;
      lit = null;
      drawChips();
      drawList();
      draw();
      if (name) {
        map.flyToBounds(L.latLngBounds(groups[name].list.map(function (i) { return [S[i].lat, S[i].lng]; })).pad(0.35), { duration: 0.6 });
      } else {
        map.flyToBounds(bounds, { duration: 0.6 });
      }
    }

    drawChips();
    draw();
    drawList();
    setTimeout(function () { map.invalidateSize(); draw(); }, 250);
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

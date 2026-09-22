/* BYKY dashboard — the parts of the design that must be computed from data:
   sparkline, month bars, emirate/category bars, deployment gauge, station map,
   and the hero carousel. Reads the same #byky-chart-data payload the old
   ApexCharts build used, so views.py is unchanged. No vendor libraries. */
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

  /* ── hero carousel ───────────────────────────────────────────── */
  (function () {
    var slides = root.querySelectorAll('.bd-hero-slide');
    var dots = root.querySelectorAll('.bd-dots button');
    if (!slides.length) return;
    var i = 0, timer;
    function go(n) {
      i = n % slides.length;
      slides.forEach(function (s, k) { s.classList.toggle('is-on', k === i); });
      dots.forEach(function (d, k) { d.classList.toggle('is-on', k === i); });
    }
    dots.forEach(function (d, k) {
      d.addEventListener('click', function () { clearInterval(timer); go(k); });
    });
    timer = setInterval(function () { go(i + 1); }, 7000);
    go(0);
  })();

  /* ── segmented range control (visual state only) ─────────────── */
  root.querySelectorAll('.bd-segment button').forEach(function (b) {
    b.addEventListener('click', function () {
      b.parentNode.querySelectorAll('button').forEach(function (o) { o.classList.remove('is-on'); });
      b.classList.add('is-on');
    });
  });

  /* ── monthly sales sparkline ─────────────────────────────────── */
  (function () {
    var svg = root.querySelector('#bd-spark');
    var series = data.daily_revenue || [];
    if (!svg || series.length < 2) return;
    var labels = data.daily_labels || [];
    var lo = Math.min.apply(null, series) * 0.9;
    var hi = Math.max.apply(null, series) * 1.04;
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
    var px = pts[peakIdx][0].toFixed(1), py = pts[peakIdx][1].toFixed(1);
    var g = el('g', { class: 'bd-spark-marker' });
    g.appendChild(el('line', { x1: px, y1: '0', x2: px, y2: '96', stroke: '#1a1640', 'stroke-width': '.8', 'stroke-dasharray': '3 3', opacity: '.28' }));
    g.appendChild(el('circle', { cx: px, cy: py, r: '3.6', fill: RED, stroke: '#fff', 'stroke-width': '2' }));
    svg.appendChild(g);

    var peakLabel = root.querySelector('#bd-spark-peak');
    if (peakLabel) peakLabel.textContent = 'Peak ' + (labels[peakIdx] || '') + ' · ' + fmt(series[peakIdx]);

    var weekend = 0, total = 0;
    series.forEach(function (v, i) {
      total += v;
      if (series[i] > (lo + hi) / 2) weekend += v;
    });
    var aux = root.querySelector('#bd-spark-aux');
    if (aux && total) aux.textContent = 'Busiest days = ' + Math.round((weekend / total) * 100) + '%';
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

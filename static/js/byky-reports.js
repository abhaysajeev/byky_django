/* BYKY reports (apps/reports) -- draws a report from its JSON and keeps the
   filters, the URL and the Export link in step.

   The page arrives with its first page in #rpt-initial. A filter change
   replaces the query string (so a refresh or a shared link shows the same
   rows), fetches data-url with it, and points Export at the same query --
   the download is exactly what the screen shows. Old rows stay, dimmed,
   while the next ones load; a failure says so inside the table block. */
(function () {
  'use strict';

  var root = document.querySelector('[data-rpt]');
  if (!root) return;

  var $ = function (sel, el) { return (el || root).querySelector(sel); };
  var $$ = function (sel, el) { return Array.prototype.slice.call((el || root).querySelectorAll(sel)); };

  var form = $('[data-rpt-form]');
  var table = $('[data-rpt-table]');
  var frame = $('[data-rpt-frame]');
  var stateBox = $('[data-rpt-state]');
  var pager = $('[data-rpt-pager]');
  var kpis = $('[data-rpt-kpis]');
  var exportLink = $('[data-rpt-export]');
  var message = $('[data-rpt-message]');
  var emptyText = ($('[data-rpt-empty]') || {}).textContent || 'Nothing found for these filters.';
  var dataUrl = root.getAttribute('data-url');
  var exportUrl = root.getAttribute('data-export');

  var current = JSON.parse(document.getElementById('rpt-initial').textContent);
  var page = current.page || 1;
  var seq = 0;

  // -- formatting ----------------------------------------------------------------------
  var money = new Intl.NumberFormat('en-US', { minimumFractionDigits: 3, maximumFractionDigits: 3 });
  var whole = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
  var NUMERIC = { money: 1, count: 1, percent: 1 };

  function shown(value, kind) {
    if (value === null || value === undefined || value === '') return null;
    if (kind === 'money') return money.format(Number(value));
    if (kind === 'count') return whole.format(Number(value));
    return String(value);
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function cell(tag, column, value, first) {
    var text = shown(value, column.kind);
    var classes = [];
    if (NUMERIC[column.kind]) classes.push('num');
    if (first) classes.push('rpt-first');
    if (text === null) classes.push('dash');
    if (column.kind === 'text') classes.push('rpt-wide');
    var node = el(tag, classes.join(' '), text === null ? '—' : text);
    if (text !== null && column.kind === 'text') node.title = text;
    return node;
  }

  // -- drawing --------------------------------------------------------------------------
  function drawKpis(data) {
    kpis.textContent = '';
    kpis.hidden = !data.kpis.length;
    kpis.style.setProperty('--kpis', data.kpis.length || 1);
    data.kpis.forEach(function (k) {
      var box = el('div', 'rpt-kpi' + (k.kind === 'money' ? ' rpt-kpi-money' : ''));
      box.appendChild(el('div', 'rpt-kpi-label', k.label + (k.kind === 'money' ? ' (AED)' : '')));
      box.appendChild(el('div', 'rpt-kpi-value', shown(k.value, k.kind) || '0'));
      kpis.appendChild(box);
    });
  }

  function drawTable(data) {
    var columns = data.columns;
    var hasLines = columns.some(function (c) { return c.part === 'line'; });
    table.textContent = '';

    var head = el('thead');
    var tr = el('tr');
    columns.forEach(function (c) { tr.appendChild(el('th', NUMERIC[c.kind] ? 'num' : '', c.label)); });
    head.appendChild(tr);
    table.appendChild(head);

    var body = el('tbody');
    var previous = 'data';
    data.rows.forEach(function (row) {
      if (data.blocks && row.kind === 'data' && previous !== 'data' && body.childNodes.length) {
        var gap = el('tr', 'rpt-gap');
        var td = el('td');
        td.colSpan = columns.length;
        gap.appendChild(td);
        body.appendChild(gap);
      }
      previous = row.kind;

      if (row.kind !== 'data') {
        var total = el('tr', row.kind === 'grand' ? 'rpt-grand' : 'rpt-total');
        columns.forEach(function (c, i) {
          var value = i === 0 ? row.label : (c.sums ? row.cells[c.key] : null);
          var td = i === 0 ? el('td', 'rpt-first', row.label) : cell('td', c, value, false);
          if (i !== 0 && !c.sums) { td.textContent = ''; td.className = ''; }
          total.appendChild(td);
        });
        body.appendChild(total);
        return;
      }

      if (!hasLines) {
        var line = el('tr');
        columns.forEach(function (c, i) { line.appendChild(cell('td', c, row.cells[c.key], i === 0)); });
        body.appendChild(line);
        return;
      }

      // An order: its own tbody, order cells spanning its vehicle rows.
      var group = el('tbody', 'rpt-order');
      var lines = row.lines && row.lines.length ? row.lines : [{}];
      lines.forEach(function (ln, i) {
        var r = el('tr');
        columns.forEach(function (c, ci) {
          if (c.part === 'line') { r.appendChild(cell('td', c, ln[c.key], false)); return; }
          if (i > 0) return;
          var td = cell('td', c, row.cells[c.key], ci === 0);
          if (lines.length > 1) td.rowSpan = lines.length;
          r.appendChild(td);
        });
        group.appendChild(r);
      });
      table.appendChild(group);
    });
    if (body.childNodes.length) table.appendChild(body);   // after the order groups: the TOTAL comes last
  }

  function drawPager(data) {
    pager.textContent = '';
    // A block report on one page says its count above the table; its row range
    // (which counts the TOTAL rows too) would only read as a second number.
    if (!data.size || (data.blocks && data.pages <= 1)) return;
    var noun = data.blocks ? 'rows' : (data.size === 1 ? data.unit[0] : data.unit[1]);
    var info = el('span');
    info.appendChild(el('b', '', whole.format(data.first) + '–' + whole.format(data.last)));
    info.appendChild(document.createTextNode(' of ' + whole.format(data.size) + ' ' + noun));
    pager.appendChild(info);
    if (data.pages <= 1) return;
    var nav = el('div', 'rpt-pager-nav');
    var prev = el('button', 'rpt-btn', 'Previous');
    var next = el('button', 'rpt-btn', 'Next');
    prev.type = next.type = 'button';
    prev.disabled = data.page <= 1;
    next.disabled = data.page >= data.pages;
    prev.addEventListener('click', function () { load(data.page - 1); });
    next.addEventListener('click', function () { load(data.page + 1); });
    nav.appendChild(prev);
    nav.appendChild(next);
    pager.appendChild(nav);
  }

  function drawScope(data) {
    var scope = $('[data-rpt-scope]');
    if (!data.scope.length) return;
    scope.textContent = '';
    data.scope.forEach(function (part) { scope.appendChild(el('span', '', part)); });
  }

  function draw(data) {
    current = data;
    page = data.page;
    drawScope(data);
    drawKpis(data);
    var found = $('[data-rpt-found]');
    found.textContent = whole.format(data.count) + ' ';
    found.appendChild(el('span', '', data.count === 1 ? data.unit[0] : data.unit[1]));
    var note = $('[data-rpt-note]');
    note.hidden = !data.note;
    note.textContent = data.note || '';
    $('[data-rpt-stamp]').textContent = 'As of ' + data.stamp;
    if (!data.count) {
      table.textContent = '';
      frame.hidden = true;
      showState(emptyText);
    } else {
      frame.hidden = false;
      stateBox.hidden = true;
      drawTable(data);
    }
    drawPager(data);
    if (exportLink) exportLink.href = exportUrl + '?' + data.query;
  }

  function showState(text, retry) {
    stateBox.textContent = text;
    if (retry) {
      var b = el('button', 'rpt-text-btn', 'Retry');
      b.type = 'button';
      b.addEventListener('click', function () { load(page); });
      stateBox.appendChild(b);
    }
    stateBox.hidden = false;
  }

  // -- filters ---------------------------------------------------------------------------
  function query(pageNo) {
    var params = new URLSearchParams();
    $$('input[name], select[name]', form).forEach(function (input) {
      var v = (input.value || '').trim();
      if (v) params.set(input.name, v);
    });
    if (pageNo && pageNo > 1) params.set('page', pageNo);
    return params.toString();
  }

  function load(pageNo) {
    var qs = query(pageNo);
    history.replaceState(null, '', qs ? '?' + qs : location.pathname);
    var mine = ++seq;
    frame.classList.add('is-loading');
    kpis.classList.add('is-loading');
    fetch(dataUrl + (qs ? '?' + qs : ''), { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (res) {
        if (res.status === 401) throw new Error('Signed out. Sign in again to see this report.');
        if (res.status === 403) throw new Error('You no longer have access to this report.');
        if (!res.ok) throw new Error('The report could not be loaded.');
        return res.json();
      })
      .then(function (data) {
        if (mine !== seq) return;
        frame.classList.remove('is-loading');
        kpis.classList.remove('is-loading');
        draw(data);
        if (pageNo && frame.scrollTo) frame.scrollTo(0, 0);
      })
      .catch(function (err) {
        if (mine !== seq) return;
        frame.classList.remove('is-loading');
        kpis.classList.remove('is-loading');
        frame.hidden = true;
        showState(err.message + ' ', true);
      });
  }

  function cascade(select) {
    var parentName = select.getAttribute('data-parent');
    var parent = parentName && form.querySelector('[name="' + parentName + '"]');
    if (!parent) return;
    var pick = parent.value;
    Array.prototype.forEach.call(select.options, function (opt) {
      if (!opt.value) return;
      var match = !pick || opt.getAttribute('data-parent-id') === pick;
      opt.hidden = !match;
      opt.disabled = !match;
      if (!match && opt.selected) select.value = '';
    });
  }

  function cascadeAll() { $$('select[data-parent]', form).forEach(cascade); }

  function countMore() {
    var badge = $('[data-rpt-applied]');
    if (!badge) return;
    var n = $$('[data-rpt-more-panel] [name]', form).filter(function (i) { return (i.value || '').trim(); }).length;
    badge.hidden = !n;
    badge.textContent = n;
  }

  var timer;
  form.addEventListener('submit', function (e) { e.preventDefault(); load(1); });
  form.addEventListener('change', function (e) {
    if (e.target.matches('select')) cascadeAll();
    if (e.target.matches('[data-rpt-text]')) return;      // typed: handled on input
    countMore();
    load(1);
  });
  form.addEventListener('input', function (e) {
    if (!e.target.matches('[data-rpt-text]')) return;
    countMore();
    clearTimeout(timer);
    timer = setTimeout(function () { load(1); }, 300);
  });

  var more = $('[data-rpt-more]');
  if (more) {
    var panel = $('[data-rpt-more-panel]');
    more.addEventListener('click', function () {
      panel.hidden = !panel.hidden;
      more.setAttribute('aria-expanded', String(!panel.hidden));
    });
    if ($$('[data-rpt-more-panel] [name]', form).some(function (i) { return (i.value || '').trim(); })) {
      panel.hidden = false;
      more.setAttribute('aria-expanded', 'true');
    }
  }

  $('[data-rpt-clear]').addEventListener('click', function () {
    $$('input[name], select[name]', form).forEach(function (input) {
      input.value = input.getAttribute('data-default') || '';
    });
    cascadeAll();
    countMore();
    load(1);
  });

  if (exportLink) {
    exportLink.addEventListener('click', function (e) {
      if (current.size > current.limit) {
        e.preventDefault();
        message.textContent = whole.format(current.size) + ' ' + current.unit[1] + ' is more than ' +
          whole.format(current.limit) + ' rows. Narrow the dates or filters and export again.';
        message.hidden = false;
      } else {
        message.hidden = true;
      }
    });
  }

  cascadeAll();
  countMore();
  draw(current);
})();

/* Navbar page search: type part of a page's name (or its module's) and open it.
 *
 * The list is the sidebar's own (#byky-nav-pages, from
 * theme/context_processors.py nav_pages), so only pages this role can open are
 * offered. Matching is in the browser -- the list is a few dozen rows.
 *
 * Ranking, best first: name starts with the query, a word in the name starts
 * with it, the name contains it, the module matches, then the query's letters
 * in order anywhere in the name ("devapp" -> Device Approval). Ties keep
 * sidebar order.
 *
 * Keys follow the ARIA combobox pattern: focus stays in the input, Up/Down
 * move the highlight (aria-activedescendant), Enter opens, Esc closes and then
 * clears. Ctrl/Cmd+K focuses the search from anywhere.
 */
(function () {
  'use strict';

  var MAX = 8;
  var box = document.querySelector('[data-nav-search]');
  var source = document.getElementById('byky-nav-pages');
  if (!box || !source) return;

  var input = box.querySelector('[data-nav-search-input]');
  var list = document.getElementById('byky-nav-results');
  var kbd = box.querySelector('.byky-nav-search-kbd');
  var pages = JSON.parse(source.textContent || '[]');
  var shown = [];
  var active = -1;

  if (kbd && /Mac|iPhone|iPad/.test(navigator.platform || '')) kbd.textContent = '⌘K';

  function escapeHtml(text) {
    return text.replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // Positions in `name` of the query's letters, in order; null if they are not all there.
  function subsequence(name, query) {
    var hits = [];
    var from = 0;
    for (var i = 0; i < query.length; i++) {
      var at = name.indexOf(query[i], from);
      if (at < 0) return null;
      hits.push(at);
      from = at + 1;
    }
    return hits;
  }

  function range(start, length) {
    var out = [];
    for (var i = 0; i < length; i++) out.push(start + i);
    return out;
  }

  // {score, hits} for one page, lower score is better; null when it does not match.
  function match(page, query) {
    var name = page.name.toLowerCase();
    var module = (page.module || '').toLowerCase();
    var at = name.indexOf(query);
    if (at === 0) return { score: 0, hits: range(0, query.length) };
    if (at > 0) {
      var word = name.search(new RegExp('\\b' + query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
      if (word >= 0) return { score: 1, hits: range(word, query.length) };
      return { score: 2, hits: range(at, query.length) };
    }
    if (module.indexOf(query) === 0) return { score: 3, hits: [] };
    if (module.indexOf(query) > 0) return { score: 4, hits: [] };
    var loose = subsequence(name, query.replace(/\s+/g, ''));
    if (loose) return { score: 5, hits: loose };
    return null;
  }

  function highlight(name, hits) {
    var marked = {};
    hits.forEach(function (i) { marked[i] = true; });
    var html = '';
    for (var i = 0; i < name.length; i++) {
      var ch = escapeHtml(name[i]);
      html += marked[i] ? '<mark>' + ch + '</mark>' : ch;
    }
    return html.replace(/<\/mark><mark>/g, '');
  }

  function setActive(index) {
    var rows = list.querySelectorAll('.byky-nav-result');
    rows.forEach(function (row, i) {
      row.classList.toggle('is-active', i === index);
      row.setAttribute('aria-selected', i === index ? 'true' : 'false');
    });
    active = index;
    if (index >= 0 && rows[index]) {
      input.setAttribute('aria-activedescendant', rows[index].id);
      rows[index].scrollIntoView({ block: 'nearest' });
    } else {
      input.removeAttribute('aria-activedescendant');
    }
  }

  function open() {
    list.hidden = false;
    input.setAttribute('aria-expanded', 'true');
  }

  function close() {
    list.hidden = true;
    input.setAttribute('aria-expanded', 'false');
    setActive(-1);
  }

  function render() {
    var query = input.value.trim().toLowerCase();
    if (!query) { close(); return; }

    shown = pages
      .map(function (page, order) {
        var found = match(page, query);
        return found && { page: page, score: found.score, hits: found.hits, order: order };
      })
      .filter(Boolean)
      .sort(function (a, b) { return a.score - b.score || a.order - b.order; })
      .slice(0, MAX);

    if (!shown.length) {
      list.innerHTML = '<div class="byky-nav-results-empty">No pages match “' + escapeHtml(input.value.trim()) + '”</div>';
    } else {
      list.innerHTML = shown.map(function (row, i) {
        return '<a class="byky-nav-result" role="option" id="byky-nav-result-' + i + '" href="' + escapeHtml(row.page.url) + '"' +
          ' aria-selected="false" tabindex="-1">' +
          '<span class="byky-nav-result-name">' + highlight(row.page.name, row.hits) + '</span>' +
          '<span class="byky-nav-result-module">' + escapeHtml(row.page.module || '') + '</span>' +
          '</a>';
      }).join('');
    }
    open();
    setActive(shown.length ? 0 : -1);
  }

  input.addEventListener('input', render);
  input.addEventListener('focus', function () { if (input.value.trim()) render(); });

  input.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (list.hidden) { render(); return; }
      if (!shown.length) return;
      e.preventDefault();
      var step = e.key === 'ArrowDown' ? 1 : -1;
      setActive((active + step + shown.length) % shown.length);
    } else if (e.key === 'Enter') {
      if (!list.hidden && active >= 0 && shown[active]) {
        e.preventDefault();
        window.location.href = shown[active].page.url;
      }
    } else if (e.key === 'Escape') {
      if (!list.hidden) { close(); } else { input.value = ''; input.blur(); }
    }
  });

  // Hover and keys share one highlight. mousedown, not click, keeps focus in
  // the input until the link is followed.
  list.addEventListener('mousemove', function (e) {
    var row = e.target.closest('.byky-nav-result');
    if (!row) return;
    var index = Array.prototype.indexOf.call(list.querySelectorAll('.byky-nav-result'), row);
    if (index !== active) setActive(index);
  });
  list.addEventListener('mousedown', function (e) { e.preventDefault(); });

  document.addEventListener('mousedown', function (e) {
    if (!box.contains(e.target)) close();
  });

  document.addEventListener('keydown', function (e) {
    if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      input.focus();
      input.select();
    }
  });
})();

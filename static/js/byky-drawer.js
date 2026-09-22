/* BYKY drawer behaviour — one file for every drawer on the site.
   1. picks compact / standard / wide from the rendered field count
   2. builds the section jump tabs (only when 2+ named sections exist)
   3. keeps the current section highlighted while you scroll
   Bootstrap's offcanvas still owns open/close. No dependencies. */
(function () {
  'use strict';

  /* Size thresholds — the only place these numbers live.
     compact:  <= 4 fields and at most one named section
     wide:     > 12 fields, or 4+ named sections
     standard: everything else */
  function pickSize(fieldCount, namedSections) {
    if (fieldCount <= 4 && namedSections <= 1) return 'is-compact';
    if (fieldCount > 12 || namedSections >= 4) return 'is-wide';
    return 'is-standard';
  }

  /* Multi-select: pills inside the control, menu below. The checkboxes stay
     the value; the pills are a view of them, rebuilt on every change, so
     prefill and reset need to know nothing about this. Works on a
     .byky-multi anywhere in the document, not just inside a drawer -- see
     the standalone init pass at the bottom of this file. */
  function initMultiselect(multi) {
    if (multi.__bykyMultiReady) return;
    multi.__bykyMultiReady = true;

    var control = multi.querySelector('.byky-multi-control');
    var menu = multi.querySelector('.byky-multi-menu');
    var pills = multi.querySelector('.byky-multi-pills');
    var placeholder = multi.querySelector('.byky-multi-placeholder');
    if (!control || !menu || !pills) return;

    function boxes() { return [].slice.call(menu.querySelectorAll('input[type="checkbox"]')); }

    function render() {
      var chosen = boxes().filter(function (b) { return b.checked; });
      pills.innerHTML = '';
      chosen.forEach(function (b) {
        var pill = document.createElement('span');
        pill.className = 'byky-pill';
        var label = document.createElement('span');
        label.textContent = b.value;
        var x = document.createElement('button');
        x.type = 'button';
        x.className = 'byky-pill-x';
        x.setAttribute('aria-label', 'Remove ' + b.value);
        x.innerHTML = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"></path></svg>';
        /* stopPropagation, or removing a pill would also open the menu */
        x.addEventListener('click', function (e) {
          e.stopPropagation();
          b.checked = false;
          render();
        });
        pill.appendChild(label);
        pill.appendChild(x);
        pills.appendChild(pill);
      });
      if (placeholder) placeholder.hidden = chosen.length > 0;
      multi.classList.toggle('has-value', chosen.length > 0);
    }

    control.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = menu.hidden;
      /* one menu at a time, including the filter dropdowns' */
      [].forEach.call(document.querySelectorAll('.byky-multi-menu'), function (m) { m.hidden = true; });
      menu.hidden = !open;
      multi.classList.toggle('is-open', !menu.hidden);
    });
    control.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); control.click(); }
    });
    menu.addEventListener('click', function (e) { e.stopPropagation(); });
    menu.addEventListener('change', render);
    document.addEventListener('click', function () {
      menu.hidden = true;
      multi.classList.remove('is-open');
    });

    multi.__bykyRenderPills = render;
    render();
  }

  function setup(drawer) {
    if (drawer.__bykyReady) return;
    drawer.__bykyReady = true;

    var body = drawer.querySelector('[data-byky-body]');
    var jumpBar = drawer.querySelector('[data-byky-jumps]');
    var sections = [].slice.call(drawer.querySelectorAll('[data-byky-section]'));
    var fields = drawer.querySelectorAll('.byky-field');
    var named = sections.filter(function (s) { return (s.dataset.sectionLabel || '').trim(); });

    /* 1 ── size (an explicit is-* class from spec.size wins) */
    if (!/\bis-(compact|standard|wide)\b/.test(drawer.className)) {
      drawer.classList.add(pickSize(fields.length, named.length));
    }

    /* selects: colour the text once a real option is chosen */
    [].forEach.call(drawer.querySelectorAll('.byky-select'), function (sel) {
      var sync = function () { sel.classList.toggle('has-value', !!sel.value); };
      sel.addEventListener('change', sync);
      sync();
    });

    /* multi-selects: pills inside the control, menu below -- see
       initMultiselect() below (extracted so a standalone .byky-multi
       outside any drawer, e.g. Fare Entry's Branch picker, can init the
       same way without needing a [data-byky-drawer] wrapper). */
    [].forEach.call(drawer.querySelectorAll('.byky-multi'), initMultiselect);

    /* file inputs: show the chosen filename */
    [].forEach.call(drawer.querySelectorAll('.byky-file-input'), function (inp) {
      inp.addEventListener('change', function () {
        var label = inp.parentNode.querySelector('span');
        if (label) label.textContent = inp.files && inp.files[0] ? inp.files[0].name : 'Choose file';
      });
    });

    if (!body || named.length < 2) return;

    /* 2 ── jump tabs */
    var tabs = named.map(function (sec, i) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'byky-jump' + (i === 0 ? ' is-on' : '');
      b.textContent = sec.dataset.sectionLabel;
      b.addEventListener('click', function () {
        lockUntil = Date.now() + 700;
        body.scrollTo({
          top: body.scrollTop + (sec.getBoundingClientRect().top - body.getBoundingClientRect().top) - 6,
          behavior: 'smooth'
        });
        mark(i);
      });
      jumpBar.appendChild(b);
      return b;
    });
    jumpBar.hidden = false;

    /* 3 ── scroll tracking. Rect deltas, not offsetTop: the drawer is a
       positioned ancestor, so offsetTop is unrelated to scroll position. */
    var lockUntil = 0;
    var current = -1;

    function mark(i) {
      if (i === current) return;
      current = i;
      tabs.forEach(function (t, k) { t.classList.toggle('is-on', k === i); });
      named.forEach(function (s, k) { s.classList.toggle('is-current', k === i); });
    }

    body.addEventListener('scroll', function () {
      if (Date.now() < lockUntil) return;
      var top = body.getBoundingClientRect().top;
      var idx = 0;
      named.forEach(function (sec, i) {
        if (sec.getBoundingClientRect().top - top <= 12) idx = i;
      });
      mark(idx);
    }, { passive: true });

    mark(0);
  }

  /* ── bridge: data-scr-open triggers and edit prefill ───────────────
     Screens on the .scr-* design system open their drawer with
     data-scr-open="<name>:add|edit" and expect an edit to arrive prefilled
     from the row's json_script blob. byky-screen.js does that for its own
     .scr-drawer element and ignores this one, so the same contract is
     honoured here: fill every [data-field], then hand open/close to
     Bootstrap. Screens using the plain data-bs-toggle trigger are untouched
     -- none of this runs unless the drawer carries data-scr-name. */
  function bridge(drawer) {
    var name = drawer.dataset.scrName;
    if (!name) return;

    var titleEl = drawer.querySelector('.byky-drawer-title');
    var addTitle = drawer.dataset.addTitle || (titleEl && titleEl.textContent) || '';
    var titleField = drawer.dataset.titleField || 'name';

    /* Conditional fields: a field with data-show-if stays hidden until the
       control it names holds one of the listed values. Re-run on every change
       and whenever the drawer opens, so edit mode shows the right shape for
       the record being edited rather than the shape the last one had. */
    function syncShowIf() {
      [].forEach.call(drawer.querySelectorAll('[data-show-if]'), function (field) {
        var spec = field.dataset.showIf;
        var sep = spec.indexOf(':');
        var key = spec.slice(0, sep);
        var wants = spec.slice(sep + 1).split('|');
        var input = drawer.querySelector('[data-field="' + key + '"]');
        var inputValue = input && input.type === 'checkbox' ? (input.checked ? 'yes' : 'no') : (input && input.value);
        var show = !!input && wants.indexOf(inputValue) > -1;
        field.hidden = !show;
        /* a hidden field must not keep a value that no longer applies */
        if (!show) {
          [].forEach.call(field.querySelectorAll('input[type="checkbox"]'), function (b) {
            b.checked = false;
          });
        }
      });
    }
    drawer.addEventListener('change', function (e) {
      if (e.target.matches('[data-field]')) syncShowIf();
    });

    function syncEnabled() {
      [].forEach.call(drawer.querySelectorAll('[data-enables]'), function (box) {
        var target = drawer.querySelector('[data-field="' + box.dataset.enables + '"]');
        if (target) target.disabled = !box.checked;
      });
    }
    [].forEach.call(drawer.querySelectorAll('[data-enables]'), function (box) {
      box.addEventListener('change', syncEnabled);
    });

    function fill(mode, record) {
      if (titleEl) {
        titleEl.textContent = mode === 'edit' && record
          ? (record[titleField] || addTitle) : addTitle;
      }
      /* multi-selects hold their value in checkboxes, not in .value */
      [].forEach.call(drawer.querySelectorAll('.byky-multi[data-field]'), function (multi) {
        var chosen = (mode === 'edit' && record && record[multi.dataset.field]) || [];
        if (typeof chosen === 'string') chosen = chosen.split(',');
        [].forEach.call(multi.querySelectorAll('input[type="checkbox"]'), function (b) {
          b.checked = chosen.indexOf(b.value) > -1;
        });
        if (multi.__bykyRenderPills) multi.__bykyRenderPills();
      });
      [].forEach.call(drawer.querySelectorAll('[data-field]'), function (el) {
        if (el.classList.contains('byky-multi')) return;
        var key = el.dataset.field;
        if (el.type === 'checkbox') {
          el.checked = mode === 'edit' && record
            ? !!record[key] : el.dataset.defaultChecked === 'true';
          return;
        }
        el.value = mode === 'edit' && record ? (record[key] || '') : (el.dataset.default || '');
        if (el.tagName === 'SELECT') el.classList.toggle('has-value', !!el.value);
        /* readOnly is meaningless on a select, so lock those with disabled */
        if (el.dataset.lockOnEdit === 'true') {
          var lock = mode === 'edit';
          if (el.tagName === 'SELECT') el.disabled = lock; else el.readOnly = lock;
        }
      });
      syncEnabled();
      syncShowIf();
    }

    drawer.__bykyFill = fill;
    fill('add', null);
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest && e.target.closest('[data-scr-open]');
    if (!btn) return;
    var parts = (btn.dataset.scrOpen || '').split(':');
    var drawer = document.querySelector('[data-scr-name="' + parts[0] + '"]');
    if (!drawer || !drawer.__bykyFill) return;   // a .scr-drawer screen: leave it alone
    var record = null;
    if (parts[1] === 'edit') {
      var row = btn.closest('.scr-row');
      var node = row && document.getElementById(row.dataset.recordId);
      record = node ? JSON.parse(node.textContent) : null;
    }
    drawer.__bykyFill(parts[1] || 'add', record);
    if (window.bootstrap && bootstrap.Offcanvas) {
      bootstrap.Offcanvas.getOrCreateInstance(drawer).show();
    }
  });

  function setupAll() {
    [].forEach.call(document.querySelectorAll('[data-byky-drawer]'), function (d) {
      setup(d);
      bridge(d);
    });
    /* a .byky-multi that isn't inside any drawer -- e.g. Fare Entry's
       Branch picker, a plain .scr-field on a full-page Tier B form --
       still gets the same pills/menu behaviour. */
    [].forEach.call(document.querySelectorAll('.byky-multi'), initMultiselect);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', setupAll);
  } else {
    setupAll();
  }

  /* re-measure when a drawer opens (fonts/plugins may have shifted layout) */
  document.addEventListener('shown.bs.offcanvas', function (e) {
    if (e.target && e.target.matches('[data-byky-drawer]')) {
      var body = e.target.querySelector('[data-byky-body]');
      if (body) body.dispatchEvent(new Event('scroll'));
    }
  });
})();

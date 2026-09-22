/* Byky RMS sidebar behaviour — vanilla, no dependencies.
   Feature parity with the design: collapse, hover-peek, drag-resize,
   accordion submenus, truncation tooltips, localStorage persistence. */
(function () {
  var MIN_W = 232, MAX_W = 400, KEY = 'byky.sidebar';

  function init(sidebar) {
    if (!sidebar || sidebar.dataset.bykyReady) return;
    sidebar.dataset.bykyReady = '1';

    var shell = sidebar.closest('.byky-shell') || sidebar.parentNode;
    var brand = sidebar.querySelector('.byky-brand-text');
    var toggle = sidebar.querySelector('.byky-toggle');
    var handle = sidebar.querySelector('.byky-handle');
    var nav = sidebar.querySelector('.byky-nav');
    var collapsed = false, width = 276, peek = false, dragging = false;

    try {
      var s = JSON.parse(localStorage.getItem(KEY) || 'null');
      if (s) {
        if (typeof s.width === 'number') width = Math.min(MAX_W, Math.max(MIN_W, s.width));
        collapsed = !!s.collapsed;
      }
    } catch (e) {}

    function persist() {
      try { localStorage.setItem(KEY, JSON.stringify({ width: width, collapsed: collapsed })); } catch (e) {}
    }
    function replayBrand() {
      if (!brand) return;
      brand.classList.remove('animate');
      void brand.offsetWidth;          // force reflow so the animation restarts
      brand.classList.add('animate');
    }
    function apply(animateBrand) {
      sidebar.style.setProperty('--sb-w', width + 'px');
      sidebar.classList.toggle('is-collapsed', collapsed);
      sidebar.classList.toggle('is-peek', collapsed && peek);
      shell.classList.toggle('is-peek', collapsed && peek);
      if (animateBrand) replayBrand();
    }
    apply(false);

    /* collapse toggle */
    if (toggle) toggle.addEventListener('click', function () {
      collapsed = !collapsed; peek = false;
      if (collapsed) closeAll();
      else (function () { var want = readOpen(); for (var i = 0; i < groups.length; i++) if (want.indexOf(groupKey(groups[i])) >= 0) groups[i].classList.add('open'); })();
      apply(!collapsed);
      persist();
    });

    /* hover-peek on the collapsed rail */
    sidebar.addEventListener('mouseenter', function () {
      if (!collapsed || peek || dragging) return;
      peek = true; apply(true);
    });
    sidebar.addEventListener('mouseleave', function () {
      hideTip();
      if (!peek) return;
      peek = false; apply(false);
    });

    /* drag to resize */
    if (handle) handle.addEventListener('pointerdown', function (e) {
      e.preventDefault();
      dragging = true;
      sidebar.classList.add('is-dragging');
      document.body.style.userSelect = 'none';
      document.body.style.cursor = 'col-resize';
    });
    window.addEventListener('pointermove', function (e) {
      if (!dragging) return;
      var left = sidebar.getBoundingClientRect().left;
      width = Math.min(MAX_W, Math.max(MIN_W, e.clientX - left));
      sidebar.style.setProperty('--sb-w', width + 'px');
    });
    window.addEventListener('pointerup', function () {
      if (!dragging) return;
      dragging = false;
      sidebar.classList.remove('is-dragging');
      document.body.style.userSelect = '';
      document.body.style.cursor = '';
      persist();
    });

    /* submenus expand INDEPENDENTLY — opening one never collapses another, so
       rows below the cursor never jump. Each group is closed by its own row.
       Open groups are remembered across page loads. */
    var groups = nav.querySelectorAll('.byky-menu-item-group');
    function groupKey(li) {
      var lbl = li.querySelector('.menu-label');
      return lbl ? lbl.textContent.trim() : '';
    }
    function readOpen() {
      try { return JSON.parse(localStorage.getItem(KEY + '.open') || '[]'); } catch (e) { return []; }
    }
    function saveOpen() {
      var out = [];
      for (var i = 0; i < groups.length; i++) if (groups[i].classList.contains('open')) out.push(groupKey(groups[i]));
      try { localStorage.setItem(KEY + '.open', JSON.stringify(out)); } catch (e) {}
    }
    function slide(li, open) {
      var ul = li.querySelector(':scope > .byky-menu-sub');
      if (!ul) return;
      var from = ul.getBoundingClientRect().height;
      ul.style.height = from + 'px';
      li.classList.toggle('open', open);
      var to = open ? ul.scrollHeight : 0;
      void ul.offsetHeight;
      ul.style.height = to + 'px';
      var done = function () {
        ul.removeEventListener('transitionend', done);
        ul.style.height = open ? 'auto' : '';
      };
      ul.addEventListener('transitionend', done);
    }
    function closeAll() {
      for (var i = 0; i < groups.length; i++) {
        groups[i].classList.remove('open');
        var ul = groups[i].querySelector(':scope > .byky-menu-sub');
        if (ul) ul.style.height = '';
      }
    }
    (function restore() {
      var want = readOpen();
      for (var i = 0; i < groups.length; i++) {
        if (want.indexOf(groupKey(groups[i])) >= 0) groups[i].classList.add('open');
      }
    })();
    nav.addEventListener('click', function (e) {
      var t = e.target.closest('.byky-menu-toggle');
      if (!t || !nav.contains(t)) return;
      e.preventDefault();
      if (collapsed && !peek) { collapsed = false; peek = false; apply(true); persist(); return; }
      if (collapsed && peek) { collapsed = false; peek = false; apply(false); persist(); }
      var li = t.parentNode;
      slide(li, !li.classList.contains('open'));
      saveOpen();
    });

    /* tooltip, only when the label is actually clipped (or the rail is collapsed) */
    var tip = null;
    function hideTip() { if (tip) { tip.remove(); tip = null; } }
    function showTip(row, text) {
      var label = row.querySelector('.menu-label');
      var clipped = (collapsed && !peek) || (label && label.scrollWidth > label.clientWidth + 1);
      if (!clipped || !text) return;
      var r = row.getBoundingClientRect();
      var edge = sidebar.getBoundingClientRect().right;
      hideTip();
      tip = document.createElement('div');
      tip.className = 'byky-tip';
      tip.textContent = text;
      tip.style.top = (r.top + r.height / 2) + 'px';
      tip.style.left = (edge + 12) + 'px';
      document.body.appendChild(tip);
    }
    nav.addEventListener('mouseover', function (e) {
      var row = e.target.closest('.byky-menu-link');
      if (!row || !nav.contains(row)) return;
      var label = row.querySelector('.menu-label');
      showTip(row, label ? label.textContent.trim() : '');
    });
    nav.addEventListener('mouseout', function (e) {
      if (e.target.closest('.byky-menu-link')) hideTip();
    });
    nav.addEventListener('scroll', hideTip);
    window.addEventListener('resize', hideTip);

    /* keep the active item's group open on load */
    var active = nav.querySelector('.byky-menu-sub .byky-menu-item.active');
    if (active) {
      var group = active.closest('.byky-menu-item.byky-menu-item-group') || active.parentNode.parentNode;
      if (group && group.classList) group.classList.add('open');
    }

    /* mobile: external button with [data-byky-open] slides the rail in */
    document.addEventListener('click', function (e) {
      if (e.target.closest('[data-byky-open]')) sidebar.classList.toggle('is-open');
      else if (window.innerWidth <= 768 && !e.target.closest('.byky-sidebar')) sidebar.classList.remove('is-open');
    });
  }

  function boot() {
    var all = document.querySelectorAll('.byky-sidebar');
    for (var i = 0; i < all.length; i++) init(all[i]);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();

/* BYKY screen design system -- the shared behaviour every redesigned screen
   extends: filter dropdowns, live search (scoped per panel/card so a tabbed
   screen's two grids filter independently), one or more Add/Edit drawers with
   their own tabs, top-level content tabs (for screens with more than one grid,
   e.g. Country & State), and "active" toggle switches. Vanilla JS, no vendor
   libraries; static data, nothing here submits or persists. Generalised from
   byky-company-details.js -- see that file's screen for the reference look. */
(function () {
  'use strict';

  var root = document.querySelector('.scr');
  if (!root) return;

  function scopeOf(el) {
    return el.closest('.scr-panel') || el.closest('.scr-card') || root;
  }

  /* ── filter dropdowns + search (scoped to the nearest panel/card) ─ */
  var filterWraps = root.querySelectorAll('.scr-filter-wrap');
  filterWraps.forEach(function (wrap) {
    var btn = wrap.querySelector('.scr-filter-btn');
    var menu = wrap.querySelector('.scr-filter-menu');
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var willOpen = menu.hidden;
      filterWraps.forEach(function (w) { w.querySelector('.scr-filter-menu').hidden = true; });
      menu.hidden = !willOpen;
    });
    menu.querySelectorAll('.scr-filter-opt').forEach(function (opt) {
      opt.addEventListener('click', function () {
        menu.querySelectorAll('.scr-filter-opt').forEach(function (o) { o.classList.remove('is-active'); });
        opt.classList.add('is-active');
        var label = btn.querySelector('.scr-filter-label');
        var value = opt.dataset.value || '';
        if (label) label.textContent = opt.textContent;
        btn.classList.toggle('is-active', !!value);
        menu.hidden = true;
        applyFilters(scopeOf(wrap));
      });
    });
  });
  document.addEventListener('click', function () {
    filterWraps.forEach(function (w) { w.querySelector('.scr-filter-menu').hidden = true; });
  });

  root.querySelectorAll('.scr-search input').forEach(function (input) {
    input.addEventListener('input', function () { applyFilters(scopeOf(input)); });
  });

  /* ── KPI tiles as filter shortcuts (data-scr-kpi-filter) ───────────
     data-scr-kpi-filter="<filter key>:<value>" on a .scr-tile/.scr-doc-item
     button clicks the matching .scr-filter-opt for that key in the same
     scope -- e.g. Employee Personal Data's Active/Blocked tiles reuse the
     existing Status filter dropdown instead of a separate mechanism. An
     empty value (e.g. "status:") clicks the dropdown's "All ..." option.

     Tiles that also share a data-scr-kpi-group (e.g. the 4 Document Expiry
     Status buttons, each a different filter key) act as one mutually-
     exclusive set: clicking one resets every other tile in the group back
     to its own "all" value first, and clicking an already-active tile
     toggles it off (back to "all") instead of re-applying it -- otherwise
     two document filters would silently AND together, which reads as
     "filter is broken" rather than "pick one document". */
  function applyKpiFilter(tile, value) {
    var sep = tile.dataset.scrKpiFilter.indexOf(':');
    var key = tile.dataset.scrKpiFilter.slice(0, sep);
    // Not scopeOf(tile): a KPI tile's own .scr-card (if it's inside one,
    // e.g. Document Expiry Status's card) is almost never the card holding
    // the rows it filters -- those live in a different .scr-card further
    // down the same screen. This mechanism is page-wide by design, so it
    // searches the whole .scr root, same as the click delegated below.
    var wrap = root.querySelector('.scr-filter-wrap[data-filter-key="' + key + '"]');
    if (!wrap) return;
    var opt = wrap.querySelector('.scr-filter-opt[data-value="' + value + '"]');
    if (!opt) return;
    opt.click();
  }
  root.querySelectorAll('[data-scr-kpi-filter]').forEach(function (tile) {
    tile.addEventListener('click', function () {
      var sep = tile.dataset.scrKpiFilter.indexOf(':');
      var key = tile.dataset.scrKpiFilter.slice(0, sep);
      var value = tile.dataset.scrKpiFilter.slice(sep + 1);
      var group = tile.dataset.scrKpiGroup;
      var wasActive = tile.classList.contains('is-active');

      if (group) {
        root.querySelectorAll('[data-scr-kpi-group="' + group + '"]').forEach(function (t) {
          if (t !== tile) applyKpiFilter(t, '');
        });
      }
      applyKpiFilter(tile, wasActive ? '' : value);

      root.querySelectorAll('[data-scr-kpi-filter]').forEach(function (t) {
        var tSep = t.dataset.scrKpiFilter.indexOf(':');
        var tKey = t.dataset.scrKpiFilter.slice(0, tSep);
        if (group && t.dataset.scrKpiGroup === group) {
          t.classList.toggle('is-active', t === tile && !wasActive);
        } else if (tKey === key) {
          t.classList.toggle('is-active', t === tile);
        }
      });
    });
  });

  /* ── pagination ───────────────────────────────────────────────────
     15 rows a page, matching the DataTables screens (byky-cms-list.js).
     Paging runs over the *filtered* set, not the raw rows, so searching
     re-pages rather than leaving gaps; any filter/search change resets to
     page 1, while clicking a page number keeps it. State is per scope, so a
     tabbed screen's two grids page independently. */
  var PAGE_SIZE = 15;
  var pageOf = new WeakMap();

  /* Page numbers to render: all of them while they fit, otherwise first and
     last with a window around the current page and ellipses for the gaps. */
  function pageList(current, total) {
    if (total <= 7) {
      return Array.apply(null, { length: total }).map(function (_, i) { return i + 1; });
    }
    var out = [1];
    var lo = Math.max(2, current - 1);
    var hi = Math.min(total - 1, current + 1);
    if (lo > 2) out.push('…');
    for (var i = lo; i <= hi; i++) out.push(i);
    if (hi < total - 1) out.push('…');
    out.push(total);
    return out;
  }

  /* The template ships prev/next buttons around a static "1". Keep the
     buttons (their icons are template-owned) and swap the static number for a
     container this owns. */
  function pagesHost(nav) {
    var host = nav.querySelector('.scr-pager-pages');
    if (!host) {
      host = document.createElement('span');
      host.className = 'scr-pager-pages';
      var stale = nav.querySelector('.scr-pager-current');
      if (stale) nav.replaceChild(host, stale);
      else nav.appendChild(host);
    }
    return host;
  }

  function renderPager(scope, page, pages) {
    scope.querySelectorAll('.scr-pager-nav').forEach(function (nav) {
      var btns = nav.querySelectorAll('.scr-pager-btn');
      var prev = btns[0];
      var next = btns[btns.length - 1];
      if (prev) {
        prev.disabled = page <= 1;
        prev.onclick = function () { goTo(scope, page - 1); };
      }
      if (next) {
        next.disabled = page >= pages;
        next.onclick = function () { goTo(scope, page + 1); };
      }

      var host = pagesHost(nav);
      host.textContent = '';
      pageList(page, pages).forEach(function (n) {
        if (n === '…') {
          var gap = document.createElement('span');
          gap.className = 'scr-pager-gap';
          gap.textContent = '…';
          host.appendChild(gap);
          return;
        }
        var b = document.createElement('button');
        b.type = 'button';
        b.className = n === page ? 'scr-pager-current' : 'scr-pager-page';
        b.textContent = n;
        if (n !== page) b.onclick = function () { goTo(scope, n); };
        host.appendChild(b);
      });
    });
  }

  function goTo(scope, page) {
    pageOf.set(scope, page);
    applyFilters(scope, true);
  }

  function applyFilters(scope, keepPage) {
    var searchInput = scope.querySelector('.scr-search input');
    var q = (searchInput && searchInput.value || '').trim().toLowerCase();
    var rows = scope.querySelectorAll('.scr-row');
    var active = {};
    scope.querySelectorAll('.scr-filter-wrap').forEach(function (w) {
      var key = w.dataset.filterKey;
      var picked = w.querySelector('.scr-filter-opt.is-active');
      active[key] = picked ? (picked.dataset.value || '') : '';
    });

    var matches = [];
    rows.forEach(function (tr) {
      var matchesQ = !q || (tr.dataset.search || '').indexOf(q) > -1;
      var matchesAll = Object.keys(active).every(function (key) {
        return !active[key] || tr.dataset[key] === active[key];
      });
      if (matchesQ && matchesAll) matches.push(tr);
    });

    var pages = Math.max(1, Math.ceil(matches.length / PAGE_SIZE));
    var page = keepPage ? (pageOf.get(scope) || 1) : 1;
    if (page > pages) page = pages;
    if (page < 1) page = 1;
    pageOf.set(scope, page);

    var start = (page - 1) * PAGE_SIZE;
    var end = start + PAGE_SIZE;
    rows.forEach(function (tr) { tr.hidden = true; });
    matches.forEach(function (tr, i) { tr.hidden = i < start || i >= end; });

    // The toolbar count is the size of the whole filtered set, not the page.
    var singular = scope.dataset.scrNounSingular || 'row';
    var plural = scope.dataset.scrNounPlural || singular + 's';

    /* A filter or search that matches nothing used to leave an empty table
       body: the {% empty %} state is rendered server-side, so it only ever
       covers "this list has no rows at all", not "none of them match". Most
       visible on Inventory Branch Mapping, where filtering to Assets is
       legitimately zero. Injected rather than added to 56 templates. */
    var tbody = scope.querySelector('tbody');
    if (tbody && rows.length) {
      var none = tbody.querySelector('[data-scr-no-match]');
      if (!matches.length) {
        if (!none) {
          none = document.createElement('tr');
          none.setAttribute('data-scr-no-match', '');
          none.innerHTML =
            '<td colspan="' + (scope.querySelectorAll('thead th').length || 1) + '" style="padding:0">' +
            '<div class="scr-empty"><span class="scr-empty-ico">' +
            '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#a9a6c4" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"></circle><path d="m20 20-3.6-3.6"></path></svg>' +
            '</span><div><div class="scr-empty-title">No matching ' + plural + '</div>' +
            '<p class="scr-empty-sub">Nothing here matches the current search and filters.</p>' +
            '</div></div></td>';
          tbody.appendChild(none);
        }
        none.hidden = false;
      } else if (none) {
        none.hidden = true;
      }
    }
    scope.querySelectorAll('.scr-toolbar-count').forEach(function (el) {
      el.textContent = matches.length + ' ' + (matches.length === 1 ? singular : plural);
    });
    scope.querySelectorAll('.scr-pager-info').forEach(function (el) {
      el.textContent = matches.length
        ? 'Showing ' + (start + 1) + '–' + Math.min(end, matches.length) + ' of ' + matches.length
        : 'Showing 0 of 0';
    });

    renderPager(scope, page, pages);
  }

  /* Paginate on load: the server renders every row, so without this a 36-row
     grid would show all 36 until the first filter interaction.

     Only leaf scopes get initialised. On a tabbed screen the .scr-card wraps
     both .scr-panels, and paging the card would treat the two grids as one
     list -- scopeOf() already resolves to the nearest panel, so the card must
     be skipped when it contains any. */
  var scopes = Array.prototype.slice.call(root.querySelectorAll('.scr-panel'));
  Array.prototype.forEach.call(root.querySelectorAll('.scr-card'), function (card) {
    if (!card.querySelector('.scr-panel')) scopes.push(card);
  });
  scopes.forEach(function (scope) {
    if (scope.querySelector('.scr-row')) applyFilters(scope);
  });

  /* ── cascading dropdowns (Country → State → Location → Branch) ────
     One shared helper, per CLAUDE.md, rather than a per-screen copy. A
     parent select declares data-scr-cascades="<child field key>"; the child
     select carries its full option list as JSON in data-scr-options (each
     entry {value, parent}) plus a plain "Select" placeholder in the markup.
     Selecting a parent value rebuilds the child's options to only those
     entries whose `parent` matches -- an unfiltered rebuild (all options)
     when the parent is cleared. Delegated at document level so it works
     inside drawers, which sit outside .scr. */
  document.addEventListener('change', function (e) {
    var parent = e.target;
    if (!(parent.matches && parent.matches('select[data-scr-cascades]'))) return;
    var childKey = parent.dataset.scrCascades;
    var scope = parent.closest('.scr-drawer, .scr-form-body, .scr') || document;
    var child = scope.querySelector('select[data-field="' + childKey + '"]');
    if (!child || !child.dataset.scrOptions) return;

    var options;
    try {
      options = JSON.parse(child.dataset.scrOptions);
    } catch (err) {
      return;
    }
    var value = parent.value;

    while (child.options.length > 1) child.remove(1);
    options.forEach(function (opt) {
      if (value && opt.parent !== value) return;
      var el = document.createElement('option');
      el.textContent = opt.value;
      child.appendChild(el);
    });
    child.value = '';
  });

  /* ── conditional field groups (data-scr-show-if) ───────────────────
     data-scr-show-if="<field key>:<value>[|<value>...]" on a .scr-drawer-field
     hides it unless the named field (another select/input in the same
     drawer/form) currently holds one of the given values -- e.g. Branch
     Management's Flags group only applies when Branch Type is "Branch
     Office". Delegated at document level for the same reason as the cascade
     listener above: drawers sit outside .scr, so a listener rooted there
     works regardless of which drawer changed. Also re-synced from
     wireDrawer's open() below, so edit mode shows the right state for the
     record being edited. */
  function syncShowIf(scope) {
    scope.querySelectorAll('[data-scr-show-if]').forEach(function (el) {
      var sep = el.dataset.scrShowIf.indexOf(':');
      var key = el.dataset.scrShowIf.slice(0, sep);
      var wants = el.dataset.scrShowIf.slice(sep + 1).split('|');
      var input = scope.querySelector('[data-field="' + key + '"]');
      el.hidden = !input || wants.indexOf(input.value) === -1;
    });
  }
  document.addEventListener('change', function (e) {
    var input = e.target;
    if (!(input.matches && input.matches('[data-field]'))) return;
    var scope = input.closest('.scr-drawer, .scr-form-body, .scr') || document;
    syncShowIf(scope);
  });

  /* ── logo / image upload preview ─────────────────────────────────
     Any input[type=file][data-scr-logo-preview="<target id>"] renders the
     chosen image into that element (an <img>, created if not already
     present). Nothing uploads anywhere -- wireframe phase, local preview
     only. Delegated at document level for the same reason as drawers: a
     preview tile can sit outside .scr (e.g. in a drawer). */
  document.addEventListener('change', function (e) {
    var input = e.target;
    if (!(input.matches && input.matches('input[type="file"][data-scr-logo-preview]'))) return;
    var file = input.files && input.files[0];
    if (!file || file.type.indexOf('image/') !== 0) return;
    var target = document.getElementById(input.dataset.scrLogoPreview);
    if (!target) return;
    var reader = new FileReader();
    reader.onload = function () {
      var img = target.querySelector('img');
      if (!img) {
        img = document.createElement('img');
        img.alt = 'Logo preview';
        target.innerHTML = '';
        target.appendChild(img);
      }
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  });

  /* ── toggle switches -- delegated at document level since drawers live
     outside .scr as siblings, not descendants ─────────────────────── */
  document.addEventListener('click', function (e) {
    var toggle = e.target.closest('.scr-toggle');
    if (!toggle) return;
    toggle.classList.toggle('is-on');
  });

  /* ── Tier D privilege matrix: per-row Select All + Remove All Privileges ─
     Shared by all 16 modules' privilege screens (byky/partials/privilege_matrix.html).
     Select All toggles every permission checkbox on its own row; Remove All
     Privileges (data-scr-clear-privileges) clears every checkbox in the
     nearest matrix table in one action, including every row's Select All. */
  document.querySelectorAll('.privilege-select-all').forEach(function (box) {
    box.addEventListener('change', function () {
      box.closest('tr').querySelectorAll('input[type="checkbox"]:not(.privilege-select-all)').forEach(function (cb) {
        cb.checked = box.checked;
      });
    });
  });

  document.querySelectorAll('[data-scr-clear-privileges]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var table = btn.closest('.scr-card').querySelector('table');
      if (!table) return;
      table.querySelectorAll('input[type="checkbox"]').forEach(function (cb) { cb.checked = false; });
    });
  });

  /* ── Tier D privilege screens: roles list <-> per-role matrix, Add/Remove
     role ────────────────────────────────────────────────────────────────
     A role is a normal Tier A list row with the same "..." kebab (Edit /
     Delete) every other list uses; Edit opens that role's screen x
     permission matrix, a full .scr-card that swaps in for the roles list
     rather than a drawer -- too many columns for a side panel. "+ Add
     Role" and each row's Delete only ever show/hide/clone pre-rendered,
     server-supplied markup -- a role not yet mapped exists as a <template>
     next to the table body and as a hidden .scr-priv-matrix card alongside
     the mapped ones' -- so nothing is fabricated at runtime (CLAUDE.md 11),
     and nothing persists past a reload (CLAUDE.md 1).

     Every actionable element here -- Edit, Delete, Add Role items -- lives
     inside a .scr-menu, which data-scr-menu-toggle detaches to a
     body-level layer while open (see wireMenuToggle's comment above), so
     by the time one is clicked it is no longer a descendant of `root` --
     delegating through `root` (or even `document`, since the menu can
     detach before either handler runs) would miss it. Every one of these
     is therefore bound directly to the element, the same reason
     data-scr-delete does, and a freshly-cloned row's own Edit/Delete/kebab
     get the identical direct wiring right after insertion. */
  document.querySelectorAll('[data-scr-privilege]').forEach(function (root) {
    var listView = root.querySelector('[data-priv-view="list"]');
    var listCard = listView && listView.querySelector('.scr-card');
    var tbody = listCard && listCard.querySelector('tbody');
    var matrices = root.querySelectorAll('.scr-priv-matrix');
    var addEmpty = root.querySelector('[data-priv-add-empty]');

    function syncAddEmpty() {
      if (!addEmpty) return;
      addEmpty.hidden = !!root.querySelector('[data-scr-priv-add]:not([hidden])');
    }
    syncAddEmpty();

    function showMatrix(role) {
      listView.hidden = true;
      matrices.forEach(function (m) { m.hidden = m.dataset.privRole !== role; });
    }
    function showList() {
      matrices.forEach(function (m) { m.hidden = true; });
      listView.hidden = false;
    }

    function wireEdit(btn) {
      btn.addEventListener('click', function () { showMatrix(btn.dataset.scrPrivEdit); });
    }

    function wireRemove(btn) {
      btn.addEventListener('click', function () {
        var row = btn.closest('[data-priv-row]');
        if (!row) return;
        var role = row.dataset.privRow;
        row.remove();
        var addBtn = root.querySelector('[data-scr-priv-add="' + role + '"]');
        if (addBtn) addBtn.hidden = false;
        syncAddEmpty();
        if (listCard) applyFilters(listCard, true);
      });
    }

    root.querySelectorAll('[data-scr-priv-edit]').forEach(wireEdit);
    root.querySelectorAll('[data-scr-priv-remove]').forEach(wireRemove);
    root.querySelectorAll('[data-scr-priv-back]').forEach(function (btn) {
      btn.addEventListener('click', showList);
    });

    root.querySelectorAll('[data-scr-priv-add]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (btn.hidden) return;
        var role = btn.dataset.scrPrivAdd;
        var tmpl = tbody && tbody.querySelector('template[data-priv-template="' + role + '"]');
        if (tmpl) {
          var clone = tmpl.content.cloneNode(true);
          var row = clone.querySelector('[data-priv-row]');
          tbody.insertBefore(clone, tmpl);
          tmpl.remove();
          if (row) {
            var toggle = row.querySelector('[data-scr-menu-toggle]');
            if (toggle) wireMenuToggle(toggle);
            row.querySelectorAll('[data-scr-priv-edit]').forEach(wireEdit);
            row.querySelectorAll('[data-scr-priv-remove]').forEach(wireRemove);
          }
        }
        btn.hidden = true;
        syncAddEmpty();
        if (listCard) applyFilters(listCard, true);
      });
    });
  });

  /* ── centered modals (data-scr-modal-open / data-scr-modal) ───────
     Distinct from the side drawers: a single-purpose action dialog (e.g.
     Device Approval / Upload APK) with no add/edit modes or record
     prefill -- just open and close. Delegated at document level so a
     trigger button anywhere on the page can reach a modal that, like
     drawers, lives outside .scr as a sibling. */
  document.querySelectorAll('[data-scr-modal-open]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var name = btn.dataset.scrModalOpen;
      var veil = document.querySelector('[data-scr-modal-veil="' + name + '"]');
      var modal = document.querySelector('[data-scr-modal="' + name + '"]');
      if (veil) veil.hidden = false;
      if (modal) modal.hidden = false;
    });
  });
  function closeModal(modal) {
    if (!modal) return;
    var name = modal.dataset.scrModal;
    var veil = name && document.querySelector('[data-scr-modal-veil="' + name + '"]');
    if (veil) veil.hidden = true;
    modal.hidden = true;
  }
  document.querySelectorAll('[data-scr-modal-close]').forEach(function (btn) {
    btn.addEventListener('click', function () { closeModal(btn.closest('[data-scr-modal]')); });
  });
  document.querySelectorAll('[data-scr-modal-veil]').forEach(function (veil) {
    veil.addEventListener('click', function () {
      var name = veil.dataset.scrModalVeil;
      closeModal(document.querySelector('[data-scr-modal="' + name + '"]'));
    });
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    document.querySelectorAll('[data-scr-modal]').forEach(function (m) {
      if (!m.hidden) closeModal(m);
    });
  });

  /* ── print trigger (data-scr-print) -- e.g. the Print button inside
     the Print Invoice bill modal. Kept as a tiny delegated handler here
     rather than an inline onclick, consistent with every other action
     being wired through a data-scr-* attribute. */
  document.querySelectorAll('[data-scr-print]').forEach(function (btn) {
    btn.addEventListener('click', function () { window.print(); });
  });

  /* ── small action dropdown menu (data-scr-menu-toggle) ────────────
     Generic open/close for a header "More actions" button, sibling to
     .scr-filter-wrap's own dropdown but not tied to filtering -- clicking
     an item just runs whatever that button does (e.g. data-scr-modal-open)
     rather than re-applying a list filter.

     A row-level "..." menu lives inside .scr-table-scroll, which sets
     overflow-x: auto -- per the CSS spec that forces overflow-y to auto too,
     so the menu's own position:absolute (relative to its .scr-menu-wrap)
     gets clipped to the scroll container's box for every row but the very
     top one, even with a high z-index -- overflow clipping is about DOM
     containment, not the positioning scheme, so position:fixed alone
     wouldn't escape it either. Fixed here by detaching the menu to a
     body-level layer on open, with real position:fixed coordinates computed
     from the toggle button's own bounding rect, then moving it back to its
     original spot in the row when closed (openMenus tracks each menu's
     original parent/next-sibling so several rows' menus don't collide). */
  var openMenus = new WeakMap();
  function closeMenu(menu) {
    menu.hidden = true;
    var orig = openMenus.get(menu);
    if (orig) {
      orig.parent.insertBefore(menu, orig.next);
      openMenus.delete(menu);
    }
  }
  function closeAllMenus() {
    document.querySelectorAll('.scr-menu').forEach(closeMenu);
  }
  var scrMenuLayer = null;
  function menuLayer() {
    if (!scrMenuLayer) {
      scrMenuLayer = document.createElement('div');
      scrMenuLayer.className = 'scr-menu-layer';
      document.body.appendChild(scrMenuLayer);
    }
    return scrMenuLayer;
  }
  /* A named declaration (hoisted within this IIFE) rather than an inline
     forEach callback, so code earlier in the file -- e.g. the privilege
     screens' Add Role, which clones a whole row including its own "..."
     kebab -- can wire a freshly-cloned toggle the same way after the page
     has already finished its initial pass over every [data-scr-menu-toggle]
     below. */
  function wireMenuToggle(btn) {
    var menu = btn.parentElement.querySelector('.scr-menu');
    if (!menu) return;
    if (btn.closest('.scr-row-actions')) menu.classList.add('scr-menu-compact');
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var willOpen = menu.hidden;
      closeAllMenus();
      if (!willOpen) return;
      openMenus.set(menu, { parent: menu.parentElement, next: menu.nextSibling });
      var rect = btn.getBoundingClientRect();
      menu.style.position = 'fixed';
      menu.style.top = (rect.bottom + 6) + 'px';
      menu.style.left = 'auto';
      menu.style.right = (window.innerWidth - rect.right) + 'px';
      menuLayer().appendChild(menu);
      menu.hidden = false;
    });
    menu.querySelectorAll('.scr-menu-item').forEach(function (item) {
      item.addEventListener('click', function () { closeMenu(menu); });
    });
  }
  document.querySelectorAll('[data-scr-menu-toggle]').forEach(wireMenuToggle);

  /* ── row delete: owned by byky-crud.js now.
     The block that stood here removed the row from the page and persisted
     nothing -- its own note said "a real delete would call an API from here
     instead". That API exists, so a second handler here would race the real
     one and could remove a row the server had just refused to delete. */

  /* ── row block / unblock (data-scr-block / data-scr-unblock) -- generic
     across any screen with a [data-status-badge] status pill: originally
     Employee Personal Data, now also Device Approval's Approved/Blocked
     tabs and its detail page. Plain instant buttons -- no confirm dialog,
     no toast (per explicit instruction: frontend-only actions like this
     stay a direct click, not a modal popup). Flips the status badge and
     data-status; nothing persisted (CLAUDE.md section 11). Falls back to
     the whole .scr-card when there's no .scr-row ancestor -- a detail page
     (Device Approval's own) has no row, only the one status pill its card
     owns. */
  function wireStatusToggle(attr, toStatus, isActive) {
    document.querySelectorAll('[' + attr + ']').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        var row = btn.closest('.scr-row, .scr-card');
        if (!row) return;
        var badge = row.querySelector('[data-status-badge]');
        var scope = scopeOf(row);

        row.dataset.status = toStatus;
        if (badge) {
          badge.classList.toggle('scr-badge-approved', isActive);
          badge.classList.toggle('scr-badge-pending', !isActive);
          badge.lastChild.textContent = toStatus;
        }
        /* Dynamic Block/Unblock -- when a row's kebab carries both (Device
           Approval's Approved/Blocked tabs), hide whichever action no
           longer applies and reveal its opposite, so a blocked device
           never offers "Block" again and vice versa. */
        var blockBtn = row.querySelector('[data-scr-block]');
        var unblockBtn = row.querySelector('[data-scr-unblock]');
        if (blockBtn) blockBtn.hidden = !isActive;
        if (unblockBtn) unblockBtn.hidden = isActive;
        applyFilters(scope, true);
      });
    });
  }
  wireStatusToggle('data-scr-block', 'Blocked', false);
  wireStatusToggle('data-scr-unblock', 'Active', true);

  /* ── manual logout (data-scr-logout) -- plain instant button, no confirm
     dialog. Device Approval's Approved/Blocked tabs use this; those queue
     devices carry no online/offline field to flip (unlike Device Mapping's
     mapped fleet, which has its own instant Logout in byky-device.js and
     does flip a status badge), so a brief non-blocking toast is the only
     feedback -- not a popup requiring a click to dismiss. */
  document.querySelectorAll('[data-scr-logout]').forEach(function (btn) {
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      if (typeof Swal === 'undefined') return;
      var row = btn.closest('.scr-row, .scr-card');
      var nameEl = row && row.querySelector('.scr-co-name, .scr-code, .scr-form-title');
      var name = nameEl ? nameEl.textContent.trim() : 'this device';
      Swal.fire({
        text: name + ' has been logged out.',
        icon: 'success',
        toast: true,
        position: 'top-end',
        showConfirmButton: false,
        timer: 2200,
        timerProgressBar: true
      });
    });
  });

  document.addEventListener('click', closeAllMenus);

  /* ── top-level content tabs (screens with more than one grid) ───── */
  document.querySelectorAll('[data-scr-tabgroup]').forEach(function (group) {
    var name = group.dataset.scrTabgroup;
    var tabs = group.querySelectorAll('.scr-content-tab');
    var panels = document.querySelectorAll('.scr-panel[data-tabgroup="' + name + '"]');
    tabs.forEach(function (t) {
      t.addEventListener('click', function () {
        tabs.forEach(function (o) { o.classList.toggle('is-on', o === t); });
        panels.forEach(function (p) { p.hidden = p.dataset.tab !== t.dataset.tab; });
      });
    });
  });

  /* ── drawers: one or more, each named via data-scr-drawer ────────── */
  var drawers = {};
  document.querySelectorAll('[data-scr-drawer]').forEach(function (el) {
    var name = el.dataset.scrDrawer;
    drawers[name] = drawers[name] || {};
    if (el.classList.contains('scr-veil')) drawers[name].veil = el;
    if (el.classList.contains('scr-drawer')) drawers[name].drawer = el;
  });

  function wireDrawer(cfg) {
    var veil = cfg.veil, drawer = cfg.drawer;
    if (!veil || !drawer) return;
    var titleEl = drawer.querySelector('.scr-drawer-title');
    var tabs = drawer.querySelectorAll('.scr-drawer-tab');
    var fieldGroups = drawer.querySelectorAll('.scr-drawer-field, .scr-drawer-hint-group');
    var toggle = drawer.querySelector('.scr-toggle');
    var addTitle = drawer.dataset.addTitle || 'Add';
    var titleField = drawer.dataset.titleField || 'name';

    function selectTab(key) {
      tabs.forEach(function (t) { t.classList.toggle('is-on', t.dataset.tab === key); });
      fieldGroups.forEach(function (g) { g.hidden = g.dataset.tab !== key; });
    }
    tabs.forEach(function (t) { t.addEventListener('click', function () { selectTab(t.dataset.tab); }); });

    /* a checkbox with data-scr-enables="<field key>" toggles that field's
       disabled state to match its own checked state (e.g. "Is Hotel" enabling
       the Hotel Commission % input) -- wired once, re-synced on every open() */
    var enablers = drawer.querySelectorAll('.scr-check[data-scr-enables]');
    enablers.forEach(function (check) {
      var target = drawer.querySelector('[data-field="' + check.dataset.scrEnables + '"]');
      function sync() { if (target) target.disabled = !check.checked; }
      check.addEventListener('change', sync);
    });

    function open(mode, record) {
      drawer.dataset.mode = mode;
      if (titleEl) titleEl.textContent = mode === 'edit' && record ? (record[titleField] || addTitle) : addTitle;
      fieldGroups.forEach(function (g) {
        var input = g.querySelector('.scr-drawer-input');
        if (!input) return;
        var key = input.dataset.field;
        input.value = mode === 'edit' && record ? (record[key] || '') : '';
        input.readOnly = mode === 'edit' && input.dataset.lockOnEdit === 'true';
      });
      /* Cascading child selects (data-scr-options) only hold a placeholder
         option until their parent's "change" fires and rebuilds the real
         list -- setting .value programmatically above does not dispatch
         that event, so the child would otherwise always land on "Select"
         in edit mode. Fire the parents now that their own values are set,
         then re-apply the record's value to each child now that it has
         real options to match against. */
      /* bubbles: true is required here -- the cascade listener is delegated
         at document level (drawers sit outside .scr), so a non-bubbling
         event never reaches it. The .scr-check listeners a few lines below
         don't need this because they're attached directly to each checkbox,
         not delegated. */
      drawer.querySelectorAll('select[data-scr-cascades]').forEach(function (p) {
        p.dispatchEvent(new Event('change', { bubbles: true }));
      });
      fieldGroups.forEach(function (g) {
        var input = g.querySelector('select[data-scr-options]');
        if (!input) return;
        input.value = mode === 'edit' && record ? (record[input.dataset.field] || '') : '';
      });
      /* checkboxes are handled as their own pass, not scoped to fieldGroups --
         a "Flags" group can hold several .scr-check elements under one label,
         and fieldGroups' querySelector (singular) would only ever reach the
         first of them. */
      drawer.querySelectorAll('.scr-check').forEach(function (check) {
        var ckey = check.dataset.field;
        check.checked = mode === 'edit' && record ? !!record[ckey] : check.dataset.defaultChecked === 'true';
        check.dispatchEvent(new Event('change'));
      });
      if (toggle) {
        var isActive = mode === 'edit' && record ? !!record.active : true;
        toggle.classList.toggle('is-on', isActive);
      }
      if (tabs.length) selectTab(tabs[0].dataset.tab);
      syncShowIf(drawer);
      veil.hidden = false;
      drawer.hidden = false;
    }
    function close() {
      veil.hidden = true;
      drawer.hidden = true;
    }
    veil.addEventListener('click', close);
    drawer._scrOpen = open;
    drawer._scrClose = close;
  }
  Object.keys(drawers).forEach(function (name) { wireDrawer(drawers[name]); });

  document.querySelectorAll('[data-scr-open]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var parts = btn.dataset.scrOpen.split(':');
      var name = parts[0], mode = parts[1] || 'add';
      var cfg = drawers[name];
      if (!cfg || !cfg.drawer || !cfg.drawer._scrOpen) return;
      var record = null;
      if (mode === 'edit') {
        var row = btn.closest('.scr-row');
        var node = row && document.getElementById(row.dataset.recordId);
        record = node ? JSON.parse(node.textContent) : null;
      }
      cfg.drawer._scrOpen(mode, record);
    });
  });
  document.querySelectorAll('[data-scr-close]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var drawer = btn.closest('.scr-drawer');
      if (drawer && drawer._scrClose) drawer._scrClose();
    });
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    Object.keys(drawers).forEach(function (name) {
      var d = drawers[name].drawer;
      if (d && !d.hidden && d._scrClose) d._scrClose();
    });
  });

  /* ── shared "view records" modal ──────────────────────────────────
     Any trigger with data-scr-view="<json-script id>" opens the shared
     #scr-records-modal (byky/partials/records_modal.html) populated from
     that JSON array, using data-scr-view-columns / -labels (JSON arrays of
     matching length) to build the table and data-scr-view-title for the
     heading. A branch's vehicle count is the first use; any future
     count-that-opens-a-list reuses this with no new markup or JS, just a
     trigger button and a json_script blob. Bootstrap's own modal JS (already
     loaded site-wide) handles show/hide; this only populates content on
     show.bs.modal, reading the triggering button via event.relatedTarget. */
  var recordsModal = document.getElementById('scr-records-modal');
  if (recordsModal) {
    var rmTitle = document.getElementById('scr-records-modal-title');
    var rmHead = document.getElementById('scr-records-thead');
    var rmBody = document.getElementById('scr-records-tbody');
    var rmEmpty = document.getElementById('scr-records-empty');
    var rmExport = document.getElementById('scr-records-export');
    var rmState = { rows: [], columns: [], labels: [], title: 'Records' };

    function csvCell(v) {
      v = v === null || v === undefined ? '' : String(v);
      return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
    }

    recordsModal.addEventListener('show.bs.modal', function (e) {
      var btn = e.relatedTarget;
      if (!btn) return;
      var node = document.getElementById(btn.dataset.scrView);
      var rows = [];
      try {
        rows = node ? JSON.parse(node.textContent) : [];
      } catch (err) {
        rows = [];
      }
      var columns = [];
      var labels = [];
      try {
        columns = JSON.parse(btn.dataset.scrViewColumns || '[]');
        labels = JSON.parse(btn.dataset.scrViewLabels || '[]');
      } catch (err2) {
        columns = [];
        labels = [];
      }
      rmState = { rows: rows, columns: columns, labels: labels, title: btn.dataset.scrViewTitle || 'Records' };

      if (rmTitle) rmTitle.textContent = rmState.title;

      rmHead.innerHTML = '';
      labels.forEach(function (label) {
        var th = document.createElement('th');
        th.textContent = label;
        rmHead.appendChild(th);
      });

      rmBody.innerHTML = '';
      rows.forEach(function (row) {
        var tr = document.createElement('tr');
        columns.forEach(function (key) {
          var td = document.createElement('td');
          td.textContent = row[key] === null || row[key] === undefined ? '' : row[key];
          tr.appendChild(td);
        });
        rmBody.appendChild(tr);
      });
      if (rmEmpty) rmEmpty.hidden = rows.length > 0;
    });

    if (rmExport) {
      rmExport.addEventListener('click', function () {
        if (!rmState.rows.length) return;
        var lines = [rmState.labels.map(csvCell).join(',')];
        rmState.rows.forEach(function (row) {
          lines.push(rmState.columns.map(function (key) { return csvCell(row[key]); }).join(','));
        });
        var blob = new Blob([lines.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url;
        a.download = (rmState.title || 'export').replace(/[^\w\- ]+/g, '').trim().replace(/\s+/g, '-') + '.csv';
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
      });
    }
  }

  /* ── shared "view one record" modal (data-scr-detail) ─────────────
     records_modal.html's sibling for a single record's key/value fields
     rather than a table of several -- see byky/partials/detail_modal.html
     for the trigger contract. */
  var detailModal = document.getElementById('scr-detail-modal');
  if (detailModal) {
    var dmTitle = document.getElementById('scr-detail-modal-title');
    var dmRows = document.getElementById('scr-detail-rows');
    var dmEmpty = document.getElementById('scr-detail-empty');

    detailModal.addEventListener('show.bs.modal', function (e) {
      var btn = e.relatedTarget;
      if (!btn) return;
      var node = document.getElementById(btn.dataset.scrDetail);
      var record = {};
      try { record = node ? JSON.parse(node.textContent) : {}; } catch (err) { record = {}; }
      var fields = [];
      try { fields = JSON.parse(btn.dataset.scrDetailFields || '[]'); } catch (err2) { fields = []; }

      if (dmTitle) dmTitle.textContent = btn.dataset.scrDetailTitle || 'Details';

      dmRows.innerHTML = '';
      var shown = 0;
      fields.forEach(function (pair) {
        var key = pair[0], label = pair[1];
        var value = record[key];
        if (value === null || value === undefined || value === '') return;
        shown++;
        var row = document.createElement('div');
        row.className = 'scr-detail-row';
        var l = document.createElement('span');
        l.className = 'scr-detail-label';
        l.textContent = label;
        var v = document.createElement('span');
        v.className = 'scr-detail-value';
        v.textContent = value;
        row.appendChild(l);
        row.appendChild(v);
        dmRows.appendChild(row);
      });
      if (dmEmpty) dmEmpty.hidden = shown > 0;
    });
  }

  /* ── search-combo (data-scr-combo, byky/partials/drawer.html's "combo"
     kind) ────────────────────────────────────────────────────────────
     A text input that filters a page-level json_script'd record list as
     you type, e.g. Duty Roster's employee search, Incentive Branch
     Mapping's plan search. data-combo-source names the json_script id;
     data-combo-key the field to match/display; data-combo-sub an optional
     second field shown alongside it. The hidden input (data-field, so it
     participates in the normal drawer fill/prefill cycle) is set to the
     chosen record's combo-key value. Re-initialised on every drawer open
     (shown.bs.offcanvas) so edit-mode prefill can re-sync the visible text
     from whatever value byky-drawer.js just wrote into the hidden field. */
  function initScrCombos(scope) {
    (scope || document).querySelectorAll('[data-scr-combo]').forEach(function (wrap) {
      if (wrap.__bykyComboInit) { wrap.__bykyComboSync(); return; }
      wrap.__bykyComboInit = true;

      var input = wrap.querySelector('.scr-combo-input');
      var list = wrap.querySelector('.scr-combo-list');
      var hidden = wrap.querySelector('input[type="hidden"]');
      var sourceEl = document.getElementById(wrap.dataset.comboSource);
      var key = wrap.dataset.comboKey || 'name';
      var subKey = wrap.dataset.comboSub || '';
      var records = [];
      try { records = sourceEl ? JSON.parse(sourceEl.textContent) : []; } catch (err) { records = []; }

      // data-combo-filter-field/-key (drawer.html's f.combo_filter_field):
      // only the records whose filterKey matches another field's current
      // value -- case-insensitively, since that field's *options* are a raw
      // choice value ("cashier") while a record's own field is its display
      // label ("Cashier"). No filter field set, or it has no value yet
      // (nothing chosen), every record shows -- exactly today's behaviour.
      var filterFieldId = wrap.dataset.comboFilterField;
      var filterKey = wrap.dataset.comboFilterKey || 'role';
      var filterControl = filterFieldId
        ? (wrap.closest('[data-byky-drawer]') || document).querySelector('[data-field="' + filterFieldId + '"]')
        : null;

      function poolRecords() {
        var want = filterControl && filterControl.value;
        if (!want) return records;
        return records.filter(function (rec) {
          return String(rec[filterKey] || '').toLowerCase() === want.toLowerCase();
        });
      }

      if (filterControl) {
        filterControl.addEventListener('change', function () {
          // The category changed -- a selection made under the old one may
          // no longer be valid, so it is cleared rather than silently kept.
          if (hidden) hidden.value = '';
          input.value = '';
          render(poolRecords());
          list.hidden = true;
        });
      }

      function label(rec) {
        var main = rec[key] || '';
        var sub = subKey ? rec[subKey] : '';
        return sub ? main + ' — ' + sub : main;
      }

      function render(items) {
        list.innerHTML = '';
        if (!items.length) {
          var empty = document.createElement('div');
          empty.className = 'scr-combo-empty';
          empty.textContent = 'No matches';
          list.appendChild(empty);
          list.hidden = false;
          return;
        }
        items.slice(0, 30).forEach(function (rec) {
          var row = document.createElement('div');
          row.className = 'scr-combo-item';
          var main = document.createElement('span');
          main.className = 'scr-combo-item-main';
          main.textContent = rec[key] || '';
          row.appendChild(main);
          if (subKey && rec[subKey]) {
            var sub = document.createElement('span');
            sub.className = 'scr-combo-item-sub';
            sub.textContent = rec[subKey];
            row.appendChild(sub);
          }
          row.addEventListener('mousedown', function (e) {
            e.preventDefault();
            if (hidden) hidden.value = rec[key] || '';
            input.value = label(rec);
            list.hidden = true;
            hidden && hidden.dispatchEvent(new Event('change', { bubbles: true }));
          });
          list.appendChild(row);
        });
        list.hidden = false;
      }

      input.addEventListener('input', function () {
        var q = input.value.trim().toLowerCase();
        if (hidden && !q) hidden.value = '';
        var pool = poolRecords();
        var items = !q ? pool : pool.filter(function (rec) {
          return (String(rec[key] || '').toLowerCase().indexOf(q) > -1)
            || (subKey && String(rec[subKey] || '').toLowerCase().indexOf(q) > -1);
        });
        render(items);
      });
      input.addEventListener('focus', function () { render(poolRecords()); });
      document.addEventListener('click', function (e) {
        if (!wrap.contains(e.target)) list.hidden = true;
      });

      wrap.__bykyComboSync = function () {
        var rec = hidden && hidden.value ? records.filter(function (r) { return (r[key] || '') === hidden.value; })[0] : null;
        input.value = rec ? label(rec) : '';
      };
    });
  }
  initScrCombos();
  document.addEventListener('shown.bs.offcanvas', function (e) {
    if (e.target) initScrCombos(e.target);
  });
})();

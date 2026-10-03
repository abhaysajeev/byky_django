/* Package add/edit page (apps/fare/templates/fare/package_form.html).

   The whole package lives here until Save posts it in one piece. Lighter than
   byky-fare.js: no live /check/ endpoint, no lock_version, no precedence
   engine -- nothing to resolve server-side until pricing/calculation is
   designed (apps/fare/package_services.py). The browser only draws the three
   grids, keeps number fields clean as they are typed, and toggles which
   fields show; the server checks everything again on save. */
(function () {
  'use strict';

  var root = document.querySelector('[data-package]');
  if (!root) return;

  var Crud = window.BykyCrud;
  var canSave = root.dataset.canSave === '1';
  var initial = JSON.parse(document.getElementById('package-initial').textContent);
  var options = JSON.parse(document.getElementById('package-options').textContent);

  var MAX_MINUTES = 1440;
  var MAX_MONEY = 9999999999.99;
  var DAY_LABEL = {
    all_days: 'All Days', monday: 'Monday', tuesday: 'Tuesday', wednesday: 'Wednesday',
    thursday: 'Thursday', friday: 'Friday', saturday: 'Saturday', sunday: 'Sunday'
  };
  var PROMOTION_LABEL = { quantity: 'Quantity', amount: 'Amount', percentage: 'Percentage', each: 'Each', package_price: 'Package Price' };
  var VEHICLE_NAME = {};
  options.vehicle_types.forEach(function (v) { VEHICLE_NAME[v.id] = v.name; });

  var state = { items: initial.items || [], free_items: initial.free_items || [], time_slabs: initial.time_slabs || [] };
  var errors = [];
  var dirty = false;
  var counter = 0;

  function $(selector, scope) { return (scope || root).querySelector(selector); }
  function $$(selector, scope) { return [].slice.call((scope || root).querySelectorAll(selector)); }
  function esc(text) {
    return String(text == null ? '' : text).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function copy(value) { return JSON.parse(JSON.stringify(value)); }
  function newKey(prefix) { counter += 1; return prefix + 'n' + counter; }
  function money(value) { var n = parseFloat(value); return isNaN(n) ? '—' : n.toFixed(2); }

  // -- Dates and times ---------------------------------------------------------------

  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function toIso(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }
  function fromIso(iso) { var p = (iso || '').split('-'); return p.length === 3 ? new Date(+p[0], +p[1] - 1, +p[2]) : null; }
  function dateLabel(iso) { var d = fromIso(iso); return d ? d.getDate() + ' ' + MONTHS[d.getMonth()] + ' ' + d.getFullYear() : '—'; }

  function picker(el, onChange) {
    if (typeof flatpickr === 'undefined') return;
    if (!el._flatpickr) flatpickr(el, { dateFormat: 'd M Y', allowInput: true });
    if (onChange) el._flatpickr.config.onChange.push(onChange);
  }
  function isoOf(el) {
    var fp = el && el._flatpickr;
    if (fp) return fp.selectedDates.length ? toIso(fp.selectedDates[0]) : '';
    return el ? el.value.trim() : '';
  }
  function setIso(el, iso) {
    if (!el) return;
    if (el._flatpickr) { if (iso) el._flatpickr.setDate(iso, false, 'Y-m-d'); else el._flatpickr.clear(false); }
    else el.value = iso || '';
  }
  function timePicker(el) {
    if (typeof flatpickr === 'undefined' || el._flatpickr) return;
    flatpickr(el, { enableTime: true, noCalendar: true, dateFormat: 'H:i', time_24hr: true,
                    allowInput: true, minuteIncrement: 1, disableMobile: true });
  }
  function hhmm(value) { var v = (value || '').trim(); return /^\d:\d\d$/.test(v) ? '0' + v : v; }
  function setTime(el, value) {
    if (el._flatpickr) { if (value) el._flatpickr.setDate(value, false, 'H:i'); else el._flatpickr.clear(false); }
    el.value = value || '';
  }

  // -- Static fields -------------------------------------------------------------

  var company = $('[data-package-field="company"]');
  var packageCode = $('[data-package-field="package_code"]');
  var packageName = $('[data-package-field="package_name"]');
  var status = $('[data-package-field="is_active"]');
  var branch = $('[data-package-field="branch"]');
  var location = $('[data-package-field="location"]');
  var branchWrap = $('[data-package-branch-wrap]');
  var locationWrap = $('[data-package-location-wrap]');
  var validFrom = $('[data-package-date="valid_from"]');
  var validTo = $('[data-package-date="valid_to"]');
  var promotionFor = $('[data-package-field="promotion_for"]');
  var inventoryType = $('[data-package-field="inventory_type"]');
  var lowerValue = $('[data-package-field="lower_value"]');
  var upperValue = $('[data-package-field="upper_value"]');
  var promotionType = $('[data-package-field="promotion_type"]');
  var freeOrPackagePrice = $('[data-package-field="free_or_package_price"]');
  var freeOrPackagePriceWrap = $('[data-package-free-price-wrap]');
  var timeSlabToggle = $('[data-package-field="time_slab_applicable"]');
  var freeItemToggle = $('[data-package-field="free_item_selectable"]');
  var freeNoteWrap = $('[data-package-free-note-wrap]');
  var freeNote = $('[data-package-field="free_item_selectable_note"]');
  var freeItemsCard = $('[data-package-free-items-card]');
  var timeSlabsCard = $('[data-package-time-slabs-card]');

  function level() { var on = $('[data-package-field="level"]:checked'); return on ? on.value : ''; }
  function toggledOn(el) { return el ? el.classList.contains('is-on') : false; }
  function setToggle(el, on) { if (el) el.classList.toggle('is-on', !!on); }

  function fillStatic() {
    if (company) company.value = initial.company || '';
    packageCode.value = initial.package_code || '';
    packageName.value = initial.package_name || '';
    status.value = initial.is_active === false ? '0' : '1';
    $$('[data-package-field="level"]').forEach(function (r) { r.checked = r.value === initial.level; });
    branch.value = initial.branch || '';
    location.value = initial.location || '';
    setIso(validFrom, initial.valid_from);
    setIso(validTo, initial.valid_to);
    promotionFor.value = initial.promotion_for || '';
    inventoryType.value = initial.inventory_type || 'vehicle_type';
    lowerValue.value = initial.lower_value || '';
    upperValue.value = initial.upper_value || '';
    promotionType.value = initial.promotion_type || '';
    freeOrPackagePrice.value = initial.free_or_package_price || 'free';
    setToggle(timeSlabToggle, initial.time_slab_applicable);
    setToggle(freeItemToggle, initial.free_item_selectable);
    freeNote.value = initial.free_item_selectable_note || '';
    applyFilters();
    showLevel();
    showRuleFields();
  }

  function showLevel() {
    var l = level();
    branchWrap.hidden = l !== 'branch';
    locationWrap.hidden = l !== 'location';
  }

  function showRuleFields() {
    var type = promotionType.value;
    var freeOn = toggledOn(freeItemToggle);
    var slabOn = toggledOn(timeSlabToggle);
    freeNoteWrap.hidden = !freeOn;
    freeOrPackagePriceWrap.hidden = !(freeOn || slabOn);
    freeItemsCard.hidden = !(type === 'quantity' && freeOn);
    timeSlabsCard.hidden = !slabOn;
    // The value column on Promotion Items is meaningless for Quantity -- the
    // mockup hides it entirely for that promotion type (models.py's own note).
    root.classList.toggle('package-hide-item-value', type === 'quantity');
    renderItems();
  }

  /* Company (system users) narrows vehicle types and branches. A hidden
     choice is cleared, never kept, same rule as the Fare page. */
  function applyFilters() {
    var companyId = company ? company.value : '';
    // The dialogs live outside `root` as siblings, same as Fare's rule dialog.
    document.querySelectorAll('select[data-item-vehicle-type], select[data-slab-vehicle-type]').forEach(function (select) {
      [].forEach.call(select.options, function (o) {
        if (!o.value) return;
        o.hidden = !!companyId && o.dataset.company !== companyId;
      });
    });
    [].forEach.call(branch.options, function (o) {
      if (!o.value) return;
      o.hidden = !!companyId && o.dataset.company !== companyId;
    });
    if (branch.selectedOptions[0] && branch.selectedOptions[0].hidden) branch.value = '';
  }

  function collect() {
    return {
      pk: initial.pk,
      company: company ? company.value : initial.company,
      package_code: packageCode.value.trim(), package_name: packageName.value.trim(),
      level: level(), branch: level() === 'branch' ? branch.value : '', location: level() === 'location' ? location.value : '',
      valid_from: isoOf(validFrom), valid_to: isoOf(validTo),
      is_active: status.value === '1',
      promotion_for: promotionFor.value, inventory_type: inventoryType.value,
      lower_value: lowerValue.value.trim(), upper_value: upperValue.value.trim(),
      promotion_type: promotionType.value,
      time_slab_applicable: toggledOn(timeSlabToggle), free_item_selectable: toggledOn(freeItemToggle),
      free_item_selectable_note: freeNote.value.trim(), free_or_package_price: freeOrPackagePrice.value,
      items: state.items, free_items: state.free_items, time_slabs: state.time_slabs
    };
  }

  // -- Promotion Items grid -----------------------------------------------------------

  function renderItems() {
    var host = $('[data-package-items]');
    var showValue = promotionType.value !== 'quantity';
    if (!state.items.length) {
      host.innerHTML = '<div class="scr-fare-empty">No promotion items yet.</div>';
      return;
    }
    host.innerHTML = '<div class="scr-table-scroll scr-sb scr-fare-table"><table><thead><tr>' +
      '<th>Vehicle Type</th><th class="scr-fare-num">Package Time</th>' + (showValue ? '<th class="scr-fare-num">Value</th>' : '') +
      (canSave ? '<th></th>' : '') + '</tr></thead><tbody>' + state.items.map(function (row) {
        return '<tr>' +
          '<td>' + esc(VEHICLE_NAME[row.vehicle_type] || 'Vehicle #' + row.vehicle_type) + '</td>' +
          '<td class="scr-fare-num">' + esc(row.package_minutes) + ' min</td>' +
          (showValue ? '<td class="scr-fare-num">' + money(row.value) + '</td>' : '') +
          (canSave ? '<td><div class="scr-fare-actions">' +
            '<button type="button" class="scr-icon-btn scr-icon-btn-edit" title="Edit" data-item-edit="' + esc(row.key) + '">' +
            '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h4l10-10a2.1 2.1 0 0 0-3-3L5 17z"></path></svg></button>' +
            '<button type="button" class="scr-icon-btn scr-icon-btn-danger" title="Delete" data-item-delete="' + esc(row.key) + '">' +
            '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2m3 0-1 13a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1L6 7h12"></path></svg></button>' +
            '</div></td>' : '') + '</tr>';
      }).join('') + '</tbody></table></div>';
  }

  function renderFreeItems() {
    var host = $('[data-package-free-items]');
    if (!state.free_items.length) {
      host.innerHTML = '<div class="scr-fare-empty">No free items yet.</div>';
      return;
    }
    host.innerHTML = '<div class="scr-table-scroll scr-sb scr-fare-table"><table><thead><tr>' +
      '<th>Vehicle Type</th><th class="scr-fare-num">Package Time</th><th class="scr-fare-num">Value</th>' +
      (canSave ? '<th></th>' : '') + '</tr></thead><tbody>' + state.free_items.map(function (row) {
        return '<tr>' +
          '<td>' + esc(VEHICLE_NAME[row.vehicle_type] || 'Vehicle #' + row.vehicle_type) + '</td>' +
          '<td class="scr-fare-num">' + esc(row.package_minutes) + ' min</td>' +
          '<td class="scr-fare-num">' + money(row.value) + '</td>' +
          (canSave ? '<td><div class="scr-fare-actions">' +
            '<button type="button" class="scr-icon-btn scr-icon-btn-edit" title="Edit" data-free-item-edit="' + esc(row.key) + '">' +
            '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h4l10-10a2.1 2.1 0 0 0-3-3L5 17z"></path></svg></button>' +
            '<button type="button" class="scr-icon-btn scr-icon-btn-danger" title="Delete" data-free-item-delete="' + esc(row.key) + '">' +
            '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2m3 0-1 13a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1L6 7h12"></path></svg></button>' +
            '</div></td>' : '') + '</tr>';
      }).join('') + '</tbody></table></div>';
  }

  function renderSlabs() {
    var host = $('[data-package-time-slabs]');
    if (!state.time_slabs.length) {
      host.innerHTML = '<div class="scr-fare-empty">No time slabs yet.</div>';
      return;
    }
    host.innerHTML = '<div class="scr-table-scroll scr-sb scr-fare-table"><table><thead><tr>' +
      '<th>Date</th><th>Day</th><th>Time</th><th>Vehicle Type</th><th class="scr-fare-num">Package Time</th><th class="scr-fare-num">Value</th>' +
      (canSave ? '<th></th>' : '') + '</tr></thead><tbody>' + state.time_slabs.map(function (row) {
        return '<tr>' +
          '<td>' + (row.date_mode === 'specific_date' ? esc(dateLabel(row.specific_date)) : 'All Dates') + '</td>' +
          '<td>' + esc(DAY_LABEL[row.day] || row.day) + '</td>' +
          '<td>' + esc(row.from) + ' – ' + esc(row.to) + '</td>' +
          '<td>' + esc(VEHICLE_NAME[row.vehicle_type] || 'Vehicle #' + row.vehicle_type) + '</td>' +
          '<td class="scr-fare-num">' + esc(row.package_minutes) + ' min</td>' +
          '<td class="scr-fare-num">' + money(row.value) + '</td>' +
          (canSave ? '<td><div class="scr-fare-actions">' +
            '<button type="button" class="scr-icon-btn scr-icon-btn-edit" title="Edit" data-slab-edit="' + esc(row.key) + '">' +
            '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h4l10-10a2.1 2.1 0 0 0-3-3L5 17z"></path></svg></button>' +
            '<button type="button" class="scr-icon-btn scr-icon-btn-danger" title="Delete" data-slab-delete="' + esc(row.key) + '">' +
            '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2m3 0-1 13a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1L6 7h12"></path></svg></button>' +
            '</div></td>' : '') + '</tr>';
      }).join('') + '</tbody></table></div>';
  }

  // -- Number fields ---------------------------------------------------------------

  function cleanNumber(el) {
    var v = el.value;
    if (el.dataset.num === 'minutes') {
      v = v.replace(/\D/g, '').replace(/^0+(?=\d)/, '');
    } else {
      v = v.replace(/[^\d.]/g, '');
      var dot = v.indexOf('.');
      if (dot !== -1) v = v.slice(0, dot + 1) + v.slice(dot + 1).replace(/\./g, '').slice(0, 2);
      v = v.replace(/^0+(?=\d)/, '');
      if (v.charAt(0) === '.') v = '0' + v;
    }
    if (v !== el.value) el.value = v;
  }

  function numberProblem(el) {
    var v = el.value.trim();
    if (!v) return 'Required.';
    if (el.dataset.num === 'minutes') {
      var n = +v, lo = +(el.dataset.min || 0), hi = +(el.dataset.max || MAX_MINUTES);
      return n < lo || n > hi ? 'Enter ' + lo + ' to ' + hi + ' minutes.' : '';
    }
    var amount = parseFloat(v);
    if (isNaN(amount)) return 'Enter an amount, such as 50.00.';
    if (amount > MAX_MONEY) return 'That amount is too large.';
    return '';
  }

  function markNumber(el, problem) {
    var field = el.closest('.scr-field');
    var note = field && field.querySelector('.scr-fare-field-error');
    el.classList.toggle('is-invalid', !!problem);
    el.setAttribute('aria-invalid', problem ? 'true' : 'false');
    if (problem && field && !note) {
      note = document.createElement('div');
      note.className = 'scr-fare-field-error';
      field.appendChild(note);
    }
    if (note) { if (problem) note.textContent = problem; else note.remove(); }
  }

  function checkNumber(el) {
    var problem = el.disabled ? '' : numberProblem(el);
    if (!problem && el.dataset.num === 'money' && el.value.trim()) {
      var tidy = parseFloat(el.value).toFixed(2);
      if (tidy !== el.value) el.value = tidy;
    }
    markNumber(el, problem);
    return !problem;
  }

  function checkNumbers(scope) {
    return $$('[data-num]', scope).filter(function (el) { return !el.disabled && el.offsetParent !== null && !checkNumber(el); }).length;
  }

  document.addEventListener('input', function (e) {
    var el = e.target;
    if (!el.dataset || !el.dataset.num) return;
    cleanNumber(el);
    if (el.classList.contains('is-invalid') && !numberProblem(el)) markNumber(el, '');
  }, true);
  document.addEventListener('focusin', function (e) {
    var el = e.target;
    if (el.dataset && el.dataset.num && !el.readOnly) setTimeout(function () { el.select(); }, 0);
  });

  function changed() { dirty = true; }

  // -- Item / free-item dialog ---------------------------------------------------

  var itemDialog = null;      // { list: 'items'|'free_items', key, isNew }
  var itemBox = document.querySelector('[data-scr-modal="package-item"]');
  var itemVehicleType = itemBox.querySelector('[data-item-vehicle-type]');
  var itemPackage = itemBox.querySelector('[data-item-package]');
  var itemValue = itemBox.querySelector('[data-item-value]');
  var itemValueWrap = itemBox.querySelector('[data-item-value-wrap]');
  var itemErrors = itemBox.querySelector('[data-item-errors]');

  function listFor(name) { return state[name]; }

  function openItem(listName, key) {
    var list = listFor(listName);
    var existing = key && list.filter(function (r) { return r.key === key; })[0];
    var row = existing ? copy(existing) : { key: newKey(listName === 'items' ? 'i' : 'f'), id: null, vehicle_type: '', package_minutes: '', value: '' };
    itemDialog = { list: listName, key: row.key, isNew: !existing };
    itemBox.querySelector('[data-item-title]').textContent = (existing ? 'Edit ' : 'Add ') +
      (listName === 'items' ? 'promotion item' : 'free item');
    itemVehicleType.value = row.vehicle_type || '';
    itemPackage.value = row.package_minutes || '';
    itemValue.value = row.value == null ? '' : row.value;
    var showValue = !(listName === 'items' && promotionType.value === 'quantity');
    itemValueWrap.hidden = !showValue;
    itemValue.disabled = !showValue;
    itemErrors.hidden = true;
    itemErrors.innerHTML = '';
    markNumber(itemPackage, ''); markNumber(itemValue, '');
    Crud.open('package-item');
  }

  function saveItem() {
    var showValue = !itemValueWrap.hidden;
    var bad = !itemVehicleType.value || checkNumbers(itemBox);
    if (bad) {
      itemErrors.innerHTML = '<li class="scr-msg-item">Fix the highlighted fields.</li>';
      itemErrors.hidden = false;
      return;
    }
    var row = {
      key: itemDialog.key, id: (listFor(itemDialog.list).filter(function (r) { return r.key === itemDialog.key; })[0] || {}).id || null,
      vehicle_type: +itemVehicleType.value, package_minutes: +itemPackage.value,
      value: showValue ? itemValue.value.trim() : null
    };
    var list = copy(listFor(itemDialog.list));
    var at = list.map(function (r) { return r.key; }).indexOf(row.key);
    if (at === -1) list.push(row); else list[at] = row;
    state[itemDialog.list] = list;
    Crud.close('package-item');
    renderItems(); renderFreeItems();
    changed();
  }

  // -- Time slab dialog -------------------------------------------------------------

  var slabDialog = null;
  var slabBox = document.querySelector('[data-scr-modal="package-slab"]');
  var slabDateWrap = slabBox.querySelector('[data-slab-date-wrap]');
  var slabDate = slabBox.querySelector('[data-slab-date]');
  var slabDay = slabBox.querySelector('[data-slab-day]');
  var slabFrom = slabBox.querySelector('[data-slab-from]');
  var slabTo = slabBox.querySelector('[data-slab-to]');
  var slabVehicleType = slabBox.querySelector('[data-slab-vehicle-type]');
  var slabPackage = slabBox.querySelector('[data-slab-package]');
  var slabValue = slabBox.querySelector('[data-slab-value]');
  var slabErrors = slabBox.querySelector('[data-slab-errors]');
  [slabFrom, slabTo].forEach(timePicker);

  function showSlabDateMode() {
    var on = slabBox.querySelector('[data-slab-date-mode]:checked');
    slabDateWrap.hidden = !on || on.value !== 'specific_date';
  }

  function openSlab(key) {
    var existing = key && state.time_slabs.filter(function (r) { return r.key === key; })[0];
    var row = existing ? copy(existing) : {
      key: newKey('t'), id: null, date_mode: 'all_dates', specific_date: '', day: 'all_days',
      from: '', to: '', vehicle_type: '', package_minutes: '', value: ''
    };
    slabDialog = { key: row.key, isNew: !existing };
    slabBox.querySelector('[data-slab-title]').textContent = (existing ? 'Edit' : 'Add') + ' time slab';
    $$('[data-slab-date-mode]', slabBox).forEach(function (r) { r.checked = r.value === row.date_mode; });
    setIso(slabDate, row.specific_date);
    slabDay.value = row.day || 'all_days';
    setTime(slabFrom, row.from);
    setTime(slabTo, row.to === '24:00' ? '00:00' : row.to);
    slabVehicleType.value = row.vehicle_type || '';
    slabPackage.value = row.package_minutes || '';
    slabValue.value = row.value == null ? '' : row.value;
    slabErrors.hidden = true;
    slabErrors.innerHTML = '';
    showSlabDateMode();
    markNumber(slabPackage, ''); markNumber(slabValue, '');
    Crud.open('package-slab');
  }

  function saveSlab() {
    var dateMode = (slabBox.querySelector('[data-slab-date-mode]:checked') || {}).value || 'all_dates';
    var specificDate = dateMode === 'specific_date' ? isoOf(slabDate) : '';
    var from = hhmm(slabFrom.value), to = hhmm(slabTo.value);
    var bad = checkNumbers(slabBox) || !slabVehicleType.value ||
      (dateMode === 'specific_date' && !specificDate) ||
      !/^\d\d:\d\d$/.test(from) || !/^\d\d:\d\d$/.test(to);
    if (!bad && to <= from) bad = true;
    if (bad) {
      slabErrors.innerHTML = '<li class="scr-msg-item">Fix the highlighted fields. The end time must be after the start time.</li>';
      slabErrors.hidden = false;
      return;
    }
    var existing = state.time_slabs.filter(function (r) { return r.key === slabDialog.key; })[0];
    var row = {
      key: slabDialog.key, id: existing ? existing.id : null, date_mode: dateMode, specific_date: specificDate,
      day: slabDay.value, from: from, to: to, vehicle_type: +slabVehicleType.value,
      package_minutes: +slabPackage.value, value: slabValue.value.trim()
    };
    var list = copy(state.time_slabs);
    var at = list.map(function (r) { return r.key; }).indexOf(row.key);
    if (at === -1) list.push(row); else list[at] = row;
    state.time_slabs = list;
    Crud.close('package-slab');
    renderSlabs();
    changed();
  }

  // -- Banner ------------------------------------------------------------------------

  function renderBanner() {
    var host = document.querySelector('[data-package-banner]');
    if (!errors.length) { host.innerHTML = ''; return; }
    host.innerHTML = '<ul class="scr-fare-banner is-error"><li class="scr-fare-banner-title">' +
      (errors.length === 1 ? 'This package can\'t be saved yet' : errors.length + ' things stop this package from saving') + '</li>' +
      errors.map(function (e) {
        return '<li>' + (e.field ? '<span class="scr-msg-field">' + esc(e.field) + '</span> ' : '') + esc(e.message) + '</li>';
      }).join('') + '</ul>';
  }

  // -- Save --------------------------------------------------------------------------

  function save(button) {
    checkNumbers(root);
    button.disabled = true;
    Crud.post(root.dataset.saveUrl, collect()).then(function (result) {
      button.disabled = false;
      var body = result.body || {};
      if (body.ok) {
        dirty = false;
        Crud.toastAfterReload(body.message);
        window.location.href = root.dataset.listUrl;
        return;
      }
      errors = body.errors || [{ field: '', message: 'Something went wrong.' }];
      renderBanner();
      if (result.status === 404) {
        Crud.showMessages({ title: 'This package no longer exists', items: errors });
      } else if (result.status === 403) {
        Crud.showMessages({ title: 'Not allowed', items: errors });
      } else {
        Crud.showMessages({ title: 'Please fix the following',
                            subtitle: errors.length + ' thing' + (errors.length === 1 ? ' needs' : 's need') + ' attention',
                            items: errors });
      }
    });
  }

  // -- Events ------------------------------------------------------------------------

  root.addEventListener('click', function (e) {
    var t = e.target;
    if (t.closest('[data-package-add-item]')) { openItem('items', null); return; }
    var editItem = t.closest('[data-item-edit]');
    if (editItem) { openItem('items', editItem.dataset.itemEdit); return; }
    var delItem = t.closest('[data-item-delete]');
    if (delItem) {
      state.items = state.items.filter(function (r) { return r.key !== delItem.dataset.itemDelete; });
      renderItems(); changed();
      return;
    }
    if (t.closest('[data-package-add-free-item]')) { openItem('free_items', null); return; }
    var editFree = t.closest('[data-free-item-edit]');
    if (editFree) { openItem('free_items', editFree.dataset.freeItemEdit); return; }
    var delFree = t.closest('[data-free-item-delete]');
    if (delFree) {
      state.free_items = state.free_items.filter(function (r) { return r.key !== delFree.dataset.freeItemDelete; });
      renderFreeItems(); changed();
      return;
    }
    if (t.closest('[data-package-add-slab]')) { openSlab(null); return; }
    var editSlab = t.closest('[data-slab-edit]');
    if (editSlab) { openSlab(editSlab.dataset.slabEdit); return; }
    var delSlab = t.closest('[data-slab-delete]');
    if (delSlab) {
      state.time_slabs = state.time_slabs.filter(function (r) { return r.key !== delSlab.dataset.slabDelete; });
      renderSlabs(); changed();
      return;
    }
    var saveButton = t.closest('[data-package-save]');
    if (saveButton) { save(saveButton); return; }
    var cancel = t.closest('[data-package-cancel]');
    if (cancel && dirty) {
      e.preventDefault();
      Crud.confirmThen({ title: 'Discard your changes?', subtitle: 'Package',
                         body: 'Nothing you changed on this page has been saved.', action: 'Discard' },
        function () { dirty = false; window.location.href = cancel.href; });
    }
  });

  root.addEventListener('change', function (e) {
    var t = e.target;
    if (t === company) applyFilters();
    if (t.dataset.packageField === 'level') showLevel();
    if (t === promotionType) showRuleFields();
    changed();
  });

  itemBox.querySelector('[data-item-save]').addEventListener('click', saveItem);
  slabBox.querySelector('[data-slab-save]').addEventListener('click', saveSlab);
  $$('[data-slab-date-mode]', slabBox).forEach(function (r) { r.addEventListener('change', showSlabDateMode); });
  [validFrom, validTo].forEach(function (el) { picker(el, changed); });

  // The toggle buttons themselves are wired by byky-screen.js's delegated
  // handler (adds/removes .is-on); this page only reacts once that happens.
  root.addEventListener('click', function (e) {
    if (e.target.closest('[data-package-field="time_slab_applicable"], [data-package-field="free_item_selectable"]')) {
      setTimeout(function () { showRuleFields(); changed(); }, 0);
    }
  });

  window.addEventListener('beforeunload', function (e) {
    if (dirty) { e.preventDefault(); e.returnValue = ''; }
  });

  // -- Start -------------------------------------------------------------------------

  fillStatic();
  renderItems();
  renderFreeItems();
  renderSlabs();
  renderBanner();
  if (!canSave) {
    $$('input, select, button.scr-toggle', root).forEach(function (el) { el.disabled = true; });
  }
  dirty = false;
})();

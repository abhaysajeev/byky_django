/* Fare add/edit page (apps/fare/templates/fare/fare_form.html).

   The whole fare lives here until Save posts it in one piece. Nothing on this
   page decides a price: overlaps, rules that never apply, holiday warnings and
   the Test fare answer all come from the server's check endpoint
   (apps.fare.services.check), which runs the same code as the save. The
   browser only draws, and checks that a time looks like HH:MM. */
(function () {
  'use strict';

  var root = document.querySelector('[data-fare]');
  if (!root) return;

  var Crud = window.BykyCrud;
  var canSave = root.dataset.canSave === '1';
  var initial = JSON.parse(document.getElementById('fare-initial').textContent);
  var options = JSON.parse(document.getElementById('fare-options').textContent);

  var KIND = { single_date: 'Single date', selected_days: 'Selected days', every_day: 'Every day' };
  var RANK = { single_date: 0, selected_days: 1, every_day: 2 };
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var PRICE = [
    ['base_fare', 'Basic fare', 'AED'], ['grace_minutes', 'Grace period', 'min'],
    ['concurrent_interval_minutes', 'Concurrent interval', 'min'],
    ['concurrent_fare', 'Concurrent fare', 'AED'], ['concurrent_grace_minutes', 'Concurrent grace', 'min']
  ];
  var DAY_SHORT = {};
  options.days.forEach(function (d) { DAY_SHORT[d.value] = d.short; });

  var state = { rules: initial.rules || [], seasons: initial.seasons || [] };
  var marks = { never: initial.never || {}, notes: initial.notes || {}, holidays: initial.holidays || [], errors: [] };
  var openSeason = null;
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
  function newKey() { counter += 1; return 'n' + counter; }

  // -- Dates and times -------------------------------------------------------

  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function toIso(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }
  function fromIso(iso) { var p = (iso || '').split('-'); return p.length === 3 ? new Date(+p[0], +p[1] - 1, +p[2]) : null; }
  function dateLabel(iso) { var d = fromIso(iso); return d ? d.getDate() + ' ' + MONTHS[d.getMonth()] + ' ' + d.getFullYear() : '—'; }
  function weekdayShort(iso) { var d = fromIso(iso); return d ? DAY_SHORT[(d.getDay() + 6) % 7] : ''; }   // Monday = 0

  /* Dates are read from the picker itself, so the value is ISO whatever the display. */
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

  // -- Labels (display only) -------------------------------------------------

  function whenLabel(rule) {
    if (rule.kind === 'every_day') return 'Every day';
    if (rule.kind === 'selected_days') {
      return options.days.filter(function (d) { return rule.weekdays.indexOf(d.value) !== -1; })
        .map(function (d) { return d.short; }).join(', ') || 'No days';
    }
    return rule.on_date ? dateLabel(rule.on_date) + ' (' + weekdayShort(rule.on_date) + ')' : 'No date';
  }
  function windowLabel(rule) {
    var end = rule.end === '00:00' ? '24:00' : rule.end;
    return rule.start === '00:00' && end === '24:00' ? 'All day' : rule.start + ' – ' + end;
  }
  function money(value) { var n = parseFloat(value); return isNaN(n) ? '—' : n.toFixed(2); }

  // -- Static fields ---------------------------------------------------------

  var company = $('[data-fare-field="company"]');
  var vehicleType = $('[data-fare-field="vehicle_type"]');
  var category = $('[data-fare-category]');
  var tax = $('[data-fare-tax]');
  var status = $('[data-fare-field="is_active"]');
  var pkg = $('[data-fare-field="package_minutes"]');
  var validFrom = $('[data-fare-date="valid_from"]');
  var validTo = $('[data-fare-date="valid_to"]');
  var branchesWrap = $('[data-fare-branches-wrap]');
  var branchBoxes = $$('[data-fare-branches] input[type="checkbox"]');

  function level() { var on = $('[data-fare-field="level"]:checked'); return on ? on.value : ''; }

  function priceFields(scope, price) {
    return '<div class="scr-grid">' + PRICE.map(function (f) {
      var id = 'fare-' + scope + '-' + f[0];
      return '<div class="scr-field"><label class="scr-label" for="' + id + '">' + f[1] + ' (' + f[2] + ')' +
        '<span class="scr-required">*</span></label><input id="' + id + '" class="scr-input" inputmode="decimal" ' +
        'data-price="' + f[0] + '" value="' + esc(price[f[0]]) + '" /></div>';
    }).join('') + '</div>';
  }
  function readPrice(host) {
    var price = {};
    $$('[data-price]', host).forEach(function (input) { price[input.dataset.price] = input.value.trim(); });
    return price;
  }

  function fillStatic() {
    if (company) company.value = initial.company || '';
    vehicleType.value = initial.vehicle_type || '';
    $$('[data-fare-field="level"]').forEach(function (r) { r.checked = r.value === initial.level; });
    status.value = initial.is_active === false ? '0' : '1';
    pkg.value = initial.package_minutes || '';
    setIso(validFrom, initial.valid_from);
    setIso(validTo, initial.valid_to);
    branchBoxes.forEach(function (box) {
      box.checked = (initial.branches || []).indexOf(+box.value) !== -1;
      box.dispatchEvent(new Event('change', { bubbles: true }));
    });
    $('[data-fare-price-host="base"]').innerHTML = priceFields('base', initial.base || {});
    applyFilters();
    showLevel();
    showTax();
  }

  function showLevel() { branchesWrap.hidden = level() !== 'branch'; }

  function showTax() {
    var option = vehicleType.selectedOptions[0];
    var value = option && option.dataset.tax;
    tax.value = value ? value + ' %' : '';
  }

  /* Company (system users) and category narrow the vehicle types; company
     also narrows the branches. A hidden choice is cleared, never kept. */
  function applyFilters() {
    var companyId = company ? company.value : '';
    var cat = category.value;
    [].forEach.call(vehicleType.options, function (o) {
      if (!o.value) return;
      o.hidden = (companyId && o.dataset.company !== companyId) || (cat && o.dataset.category !== cat);
    });
    if (vehicleType.selectedOptions[0] && vehicleType.selectedOptions[0].hidden) vehicleType.value = '';
    $$('[data-fare-branches] .byky-multi-opt').forEach(function (label) {
      var hide = !!companyId && label.dataset.company !== companyId;
      label.hidden = hide;
      var box = label.querySelector('input');
      if (hide && box.checked) { box.checked = false; box.dispatchEvent(new Event('change', { bubbles: true })); }
    });
  }

  function collect(rules, seasons) {
    return {
      pk: initial.pk, lock_version: initial.lock_version,
      company: company ? company.value : initial.company,
      vehicle_type: vehicleType.value, level: level(),
      branches: level() === 'branch'
        ? branchBoxes.filter(function (b) { return b.checked; }).map(function (b) { return +b.value; }) : [],
      valid_from: isoOf(validFrom), valid_to: isoOf(validTo),
      is_active: status.value === '1', package_minutes: pkg.value.trim(),
      base: readPrice($('[data-fare-price-host="base"]')),
      rules: rules || state.rules, seasons: seasons || state.seasons
    };
  }

  // -- Special prices ----------------------------------------------------------

  function season(key) { return state.seasons.filter(function (s) { return s.key === key; })[0]; }
  function rulesOf(owner) { return owner === 'fare' ? state.rules : season(owner).rules; }

  function renderRules(owner, host) {
    if (!host) return;
    var rules = rulesOf(owner).slice().sort(function (a, b) {
      return RANK[a.kind] - RANK[b.kind] || (a.on_date || '').localeCompare(b.on_date || '') ||
             (a.start || '').localeCompare(b.start || '');
    });
    if (!rules.length) {
      host.innerHTML = '<div class="scr-fare-empty">No special prices. The ' +
        (owner === 'fare' ? 'base fare' : "season's base fare") + ' applies all day.</div>';
      return;
    }
    host.innerHTML = '<div class="scr-table-scroll scr-sb scr-fare-table"><table><thead><tr>' +
      '<th>Applies on</th><th>Time</th><th class="scr-fare-num">Basic fare</th><th class="scr-fare-num">Grace</th>' +
      '<th class="scr-fare-num">Interval</th><th class="scr-fare-num">Conc. fare</th><th class="scr-fare-num">Conc. grace</th>' +
      (canSave ? '<th></th>' : '') + '</tr></thead><tbody>' + rules.map(function (r) {
        var never = marks.never[r.key], note = marks.notes[r.key];
        return '<tr>' +
          '<td>' + (r.kind === 'every_day' ? '' : '<span class="scr-fare-kind">' + KIND[r.kind] + '</span>') +
          '<b>' + esc(whenLabel(r)) + '</b>' +
          (note ? '<span class="scr-fare-note">' + esc(note) + '</span>' : '') +
          (never ? '<span class="scr-fare-warn">Never applies</span><span class="scr-fare-warn-why">' + esc(never) + '</span>' : '') +
          '</td><td>' + esc(windowLabel(r)) + '</td>' +
          '<td class="scr-fare-num">' + money(r.price.base_fare) + '</td>' +
          '<td class="scr-fare-num">' + esc(r.price.grace_minutes) + ' min</td>' +
          '<td class="scr-fare-num">' + esc(r.price.concurrent_interval_minutes) + ' min</td>' +
          '<td class="scr-fare-num">' + money(r.price.concurrent_fare) + '</td>' +
          '<td class="scr-fare-num">' + esc(r.price.concurrent_grace_minutes) + ' min</td>' +
          (canSave ? '<td><div class="scr-fare-actions">' +
            '<button type="button" class="scr-icon-btn scr-icon-btn-edit" title="Edit" data-rule-edit="' + esc(r.key) + '" data-owner="' + esc(owner) + '">' +
            '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h4l10-10a2.1 2.1 0 0 0-3-3L5 17z"></path></svg></button>' +
            '<button type="button" class="scr-icon-btn scr-icon-btn-danger" title="Delete" data-rule-delete="' + esc(r.key) + '" data-owner="' + esc(owner) + '">' +
            '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2m3 0-1 13a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1L6 7h12"></path></svg></button>' +
            '</div></td>' : '') + '</tr>';
      }).join('') + '</tbody></table></div>';
  }

  // -- Seasons ---------------------------------------------------------------

  function seasonDays(s) {
    var a = fromIso(s.start_date), b = fromIso(s.end_date);
    return a && b && b >= a ? Math.round((b - a) / 864e5) + 1 : 0;
  }
  function seasonMeta(s) {
    var days = seasonDays(s);
    var hidden = s.rules.filter(function (r) { return marks.never[r.key]; }).length;
    return dateLabel(s.start_date) + ' – ' + dateLabel(s.end_date) + (days ? ' · ' + days + ' day' + (days === 1 ? '' : 's') : '') +
      ' · base AED ' + money(s.base.base_fare) + ' · ' + s.rules.length + ' special price' + (s.rules.length === 1 ? '' : 's') +
      (hidden ? ' · <span class="scr-fare-warn-inline">' + hidden + ' never appl' + (hidden === 1 ? 'ies' : 'y') + '</span>' : '');
  }

  function renderSeasons() {
    var host = $('[data-fare-seasons]');
    if (!state.seasons.length) {
      host.innerHTML = '<div class="scr-fare-empty">No seasons. The fare\'s own prices apply on every date.</div>';
      return;
    }
    var fareRules = state.rules.filter(function (r) { return r.kind !== 'single_date'; });
    host.innerHTML = state.seasons.map(function (s) {
      var open = openSeason === s.key;
      var singles = state.rules.filter(function (r) {
        return r.kind === 'single_date' && r.on_date && s.start_date && r.on_date >= s.start_date && r.on_date <= s.end_date;
      });
      return '<section class="scr-fare-season' + (open ? ' is-open' : '') + '" data-season="' + esc(s.key) + '">' +
        '<div class="scr-fare-season-head">' +
          '<div><b data-season-name>' + esc(s.name || 'New season') + '</b><span class="scr-fare-season-meta" data-season-meta>' + seasonMeta(s) + '</span></div>' +
          '<div class="scr-fare-season-actions">' +
            '<button type="button" class="scr-btn scr-btn-small" data-season-toggle>' + (open ? 'Done' : (canSave ? 'Edit' : 'View')) + '</button>' +
            (canSave ? '<button type="button" class="scr-icon-btn scr-icon-btn-danger" title="Delete season" data-season-delete>' +
              '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2m3 0-1 13a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1L6 7h12"></path></svg></button>' : '') +
          '</div>' +
        '</div>' +
        (open ? '<div class="scr-fare-season-body">' +
          '<div class="scr-grid">' +
            '<div class="scr-field"><label class="scr-label" for="season-name-' + esc(s.key) + '">Season name<span class="scr-required">*</span></label>' +
              '<input id="season-name-' + esc(s.key) + '" class="scr-input" data-season-field="name" value="' + esc(s.name) + '" /></div>' +
            '<div class="scr-field"><label class="scr-label" for="season-from-' + esc(s.key) + '">From date<span class="scr-required">*</span></label>' +
              '<input id="season-from-' + esc(s.key) + '" class="scr-input byky-date" data-season-date="start_date" placeholder="Select date" /></div>' +
            '<div class="scr-field"><label class="scr-label" for="season-to-' + esc(s.key) + '">To date<span class="scr-required">*</span></label>' +
              '<input id="season-to-' + esc(s.key) + '" class="scr-input byky-date" data-season-date="end_date" placeholder="Select date" /></div>' +
          '</div>' +
          '<h3 class="scr-fare-sub">Season base fare</h3>' +
          '<div data-season-price>' + priceFields('season-' + s.key, s.base) + '</div>' +
          '<div class="scr-fare-sub-row"><h3 class="scr-fare-sub">Special pricing in ' + esc(s.name || 'this season') + '</h3>' +
            (canSave ? '<button type="button" class="scr-btn scr-btn-small" data-fare-add-rule="' + esc(s.key) + '">Add price</button>' : '') + '</div>' +
          '<p class="scr-help scr-fare-season-help">Selected days → Every day → season base fare.' +
            (fareRules.length ? " The fare's own special prices are not used on these dates." : '') +
            (singles.length ? ' Single dates still apply here: ' + singles.map(function (r) { return esc(dateLabel(r.on_date)); }).join(', ') + '.' : '') + '</p>' +
          '<div data-fare-rules="' + esc(s.key) + '"></div>' +
        '</div>' : '') +
      '</section>';
    }).join('');

    var body = openSeason && $('[data-season="' + openSeason + '"] .scr-fare-season-body');
    if (body) {
      var s = season(openSeason);
      $$('[data-season-date]', body).forEach(function (el) {
        picker(el, function () { s[el.dataset.seasonDate] = isoOf(el); refreshSeasonHead(s); changed(); });
        setIso(el, s[el.dataset.seasonDate]);
      });
      if (!canSave) $$('input, select', body).forEach(function (el) { el.disabled = true; });
    }
    renderAllRules();
  }

  function refreshSeasonHead(s) {
    var section = $('[data-season="' + s.key + '"]');
    if (!section) return;
    section.querySelector('[data-season-name]').textContent = s.name || 'New season';
    section.querySelector('[data-season-meta]').innerHTML = seasonMeta(s);
  }

  function renderAllRules() {
    renderRules('fare', $('[data-fare-rules="fare"]'));
    state.seasons.forEach(function (s) { renderRules(s.key, $('[data-fare-rules="' + s.key + '"]')); });
  }

  // -- The live check --------------------------------------------------------

  var checkTimer = null;
  var checkSeq = 0;

  function applyMarks(body) {
    marks.never = body.never || {};
    marks.notes = body.notes || {};
    marks.holidays = body.holidays || [];
    marks.errors = body.errors || [];
    renderAllRules();
    state.seasons.forEach(refreshSeasonHead);
    renderBanner();
  }

  function scheduleCheck() {
    clearTimeout(checkTimer);
    checkTimer = setTimeout(function () {
      var seq = ++checkSeq;
      Crud.post(root.dataset.checkUrl, { fare: collect() }).then(function (result) {
        if (seq === checkSeq && result.body && result.body.ok) applyMarks(result.body);
      });
    }, 400);
  }

  function renderBanner() {
    var host = $('[data-fare-banner]');
    var never = Object.keys(marks.never).length;
    var items = [];
    marks.holidays.forEach(function (h) { items.push('<li>' + esc(h.message) + '</li>'); });
    if (never) {
      items.push('<li>' + never + ' special price' + (never === 1 ? ' never applies' : 's never apply') +
        '. It is saved, but will never be charged. See the rows marked below.</li>');
    }
    host.innerHTML = items.length ? '<ul class="scr-fare-banner">' + items.join('') + '</ul>' : '';
  }

  function changed() { dirty = true; scheduleCheck(); }

  // -- The special-price dialog -------------------------------------------------

  var dialog = null;
  var ruleBox = document.querySelector('[data-scr-modal="fare-rule"]');
  var ruleKind = ruleBox.querySelector('[data-rule-kind]');
  var ruleDate = ruleBox.querySelector('[data-rule-date]');
  var ruleFrom = ruleBox.querySelector('[data-rule-from]');
  var ruleTo = ruleBox.querySelector('[data-rule-to]');
  var ruleErrors = ruleBox.querySelector('[data-rule-errors]');
  var rulePriceHost = ruleBox.querySelector('[data-fare-price-host="rule"]');
  ruleBox.querySelector('[data-rule-days]').innerHTML = options.days.map(function (d) {
    return '<label class="scr-fare-day"><input type="checkbox" value="' + d.value + '" /><span>' + d.short + '</span></label>';
  }).join('');
  [ruleFrom, ruleTo].forEach(timePicker);

  function showKind() {
    var kind = ruleKind.value;
    ruleBox.querySelector('[data-rule-days-wrap]').hidden = kind !== 'selected_days';
    ruleBox.querySelector('[data-rule-date-wrap]').hidden = kind !== 'single_date';
  }

  function openRule(owner, key) {
    var existing = key && rulesOf(owner).filter(function (r) { return r.key === key; })[0];
    var base = owner === 'fare' ? readPrice($('[data-fare-price-host="base"]')) : season(owner).base;
    var rule = existing ? copy(existing) : {
      key: newKey(), id: null, kind: owner === 'fare' ? 'single_date' : 'every_day', weekdays: [],
      on_date: '', start: '', end: '', price: copy(base)
    };
    dialog = { owner: owner, rule: rule, isNew: !existing };

    ruleBox.querySelector('[data-rule-title]').textContent = (existing ? 'Edit' : 'Add') + ' special price';
    ruleBox.querySelector('[data-rule-sub]').textContent = owner === 'fare' ? 'This fare' : 'Season · ' + (season(owner).name || 'New season');
    ruleKind.querySelector('option[value="single_date"]').hidden = owner !== 'fare';
    ruleBox.querySelector('[data-rule-kind-help]').textContent = owner === 'fare' ? '' :
      'Single dates are added on the fare, not inside a season. They apply here too.';
    ruleKind.value = rule.kind;
    $$('input', ruleBox.querySelector('[data-rule-days]')).forEach(function (box) {
      box.checked = rule.weekdays.indexOf(+box.value) !== -1;
    });
    setIso(ruleDate, rule.on_date);
    setTime(ruleFrom, rule.start);
    setTime(ruleTo, rule.end === '24:00' ? '00:00' : rule.end);
    rulePriceHost.innerHTML = priceFields('rule', rule.price);
    ruleErrors.hidden = true;
    showKind();
    Crud.open('fare-rule');
  }

  function readRule() {
    var rule = copy(dialog.rule);
    rule.kind = ruleKind.value;
    rule.weekdays = rule.kind === 'selected_days'
      ? $$('input:checked', ruleBox.querySelector('[data-rule-days]')).map(function (b) { return +b.value; }) : [];
    rule.on_date = rule.kind === 'single_date' ? isoOf(ruleDate) : '';
    rule.start = hhmm(ruleFrom.value);
    rule.end = hhmm(ruleTo.value);
    rule.price = readPrice(rulePriceHost);
    return rule;
  }

  function withRule(rule) {
    var rules = copy(state.rules), seasons = copy(state.seasons);
    var list = dialog.owner === 'fare' ? rules : seasons.filter(function (s) { return s.key === dialog.owner; })[0].rules;
    var at = list.map(function (r) { return r.key; }).indexOf(rule.key);
    if (at === -1) list.push(rule); else list[at] = rule;
    return { rules: rules, seasons: seasons };
  }

  /* The dialog asks the server about the fare with and without this price,
     and shows only the problems this price adds -- so an unrelated error
     elsewhere on the page never blocks it. */
  function saveRule() {
    var rule = readRule();
    var next = withRule(rule);
    var url = root.dataset.checkUrl;
    Promise.all([
      Crud.post(url, { fare: collect() }),
      Crud.post(url, { fare: collect(next.rules, next.seasons) })
    ]).then(function (results) {
      var before = {};
      (results[0].body.errors || []).forEach(function (e) { before[e.key + '|' + e.field + '|' + e.message] = true; });
      var added = (results[1].body.errors || []).filter(function (e) { return !before[e.key + '|' + e.field + '|' + e.message]; });
      if (added.length) {
        ruleErrors.innerHTML = added.map(function (e) {
          return '<li class="scr-msg-item"><span class="scr-msg-field">' + esc(e.field) + '</span> ' + esc(e.message) + '</li>';
        }).join('');
        ruleErrors.hidden = false;
        return;
      }
      state.rules = next.rules;
      state.seasons = next.seasons;
      dirty = true;
      Crud.close('fare-rule');
      renderSeasons();
      applyMarks(results[1].body);
    });
  }

  // -- Test fare -------------------------------------------------------------------

  var testBox = document.querySelector('[data-scr-modal="fare-test"]');
  var testDate = testBox.querySelector('[data-test-date]');
  var testTime = testBox.querySelector('[data-test-time]');
  var testResult = testBox.querySelector('[data-test-result]');
  timePicker(testTime);

  function summary() {
    var type = vehicleType.selectedOptions[0];
    var names = branchBoxes.filter(function (b) { return b.checked; })
      .map(function (b) { return b.parentElement.querySelector('span').textContent; });
    var rows = [
      ['Vehicle type', type && type.value ? type.textContent : '—'],
      ['Category', type && type.value ? type.dataset.category : '—'],
      ['Package time', pkg.value ? pkg.value + ' min' : '—'],
      ['Fare level', level() === 'branch' ? 'Branch' : 'Company'],
      ['Applies to', level() === 'branch' ? (names.join(', ') || '—') : 'All branches'],
      ['Valid', dateLabel(isoOf(validFrom)) + ' – ' + dateLabel(isoOf(validTo))],
      ['Tax', tax.value || '— not set —'],
      ['Status', status.value === '1' ? 'Active' : 'Inactive']
    ];
    testBox.querySelector('[data-test-summary]').innerHTML = rows.map(function (r) {
      return '<div><dt>' + r[0] + '</dt><dd>' + esc(r[1]) + '</dd></div>';
    }).join('');
  }

  function timeline(schedule, minute) {
    return '<div class="scr-fare-timeline" role="img" aria-label="The whole day">' + schedule.map(function (s) {
      var width = (s.end - s.start) / 14.4;
      return '<span class="scr-fare-seg is-' + s.source.tier + (s.source.season && s.source.tier === 'base' ? ' is-season' : '') +
        '" style="width:' + width + '%" title="' + esc(s.from + ' – ' + s.to + ' · AED ' + s.base_fare + ' · ' + s.source.label) + '">' +
        (width > 9 ? '<span>' + esc(s.base_fare) + '</span>' : '') + '</span>';
    }).join('') + '<i class="scr-fare-marker" style="left:' + (minute / 14.4) + '%"></i></div>' +
      '<div class="scr-fare-axis"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div>';
  }

  function runTest() {
    var time = hhmm(testTime.value);
    Crud.post(root.dataset.checkUrl, { fare: collect(), test: { date: isoOf(testDate), time: time } }).then(function (result) {
      var body = result.body || {};
      if (body.errors && body.errors.length && !body.test) {
        testResult.innerHTML = '<div class="scr-fare-result is-none"><b>Fix the fare first</b><ul class="scr-msg-list">' +
          body.errors.map(function (e) { return '<li class="scr-msg-item"><span class="scr-msg-field">' + esc(e.field) + '</span> ' + esc(e.message) + '</li>'; }).join('') +
          '</ul></div>';
        return;
      }
      var t = body.test;
      if (!t) { testResult.innerHTML = ''; return; }
      if (!t.found) {
        testResult.innerHTML = '<div class="scr-fare-result is-none"><b>' + esc(t.day ? 'No fare on ' + t.day : 'Nothing to show') + '</b><p>' + esc(t.message) + '</p></div>';
        return;
      }
      var p = t.price;
      var parts = time.split(':');
      var minute = (+parts[0]) * 60 + (+parts[1]);
      testResult.innerHTML = '<div class="scr-fare-result">' +
        '<div class="scr-fare-result-top"><div><span class="scr-fare-result-when">' + esc(time + ' · ' + t.day) + '</span>' +
          '<div class="scr-fare-result-source">' + esc(t.source.label) + '</div>' +
          '<div class="scr-fare-result-block">Applies ' + esc(t.block) + '</div></div>' +
          '<div class="scr-fare-result-amount"><small>AED</small>' + esc(p.base_fare) + '</div></div>' +
        '<dl class="scr-fare-result-grid">' +
          '<div><dt>Package</dt><dd>' + esc(pkg.value) + ' min</dd></div>' +
          '<div><dt>Grace</dt><dd>' + esc(p.grace_minutes) + ' min</dd></div>' +
          '<div><dt>Then every</dt><dd>' + esc(p.concurrent_interval_minutes) + ' min</dd></div>' +
          '<div><dt>Adds</dt><dd>AED ' + esc(p.concurrent_fare) + '</dd></div>' +
          '<div><dt>Conc. grace</dt><dd>' + esc(p.concurrent_grace_minutes) + ' min</dd></div>' +
        '</dl>' +
        (t.also.length ? '<ul class="scr-fare-also">' + t.also.map(function (a) {
          return '<li><b>Also matches:</b> ' + esc(a.label) + ' (' + esc(a.reason) + ')</li>';
        }).join('') + '</ul>' : '') +
        '<div class="scr-fare-result-day"><span class="scr-fare-result-when">The whole day</span>' + timeline(t.schedule, minute) + '</div>' +
      '</div>';
    });
  }

  function openTest() {
    summary();
    if (!isoOf(testDate)) {
      var from = isoOf(validFrom), today = toIso(new Date());
      setIso(testDate, from && today < from ? from : today);
    }
    if (!testTime.value) {
      var now = new Date();
      setTime(testTime, pad(now.getHours()) + ':' + pad(now.getMinutes()));
    }
    Crud.open('fare-test');
    runTest();
  }

  // -- Save ------------------------------------------------------------------------

  function save(button) {
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
      var errors = body.errors || [{ field: '', message: 'Something went wrong.' }];
      if (result.status === 409) {
        Crud.showMessages({ title: 'This fare was changed by someone else', items: errors, note: body.note });
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
    var add = t.closest('[data-fare-add-rule]');
    if (add) { openRule(add.dataset.fareAddRule, null); return; }
    var edit = t.closest('[data-rule-edit]');
    if (edit) { openRule(edit.dataset.owner, edit.dataset.ruleEdit); return; }
    var del = t.closest('[data-rule-delete]');
    if (del) {
      var list = rulesOf(del.dataset.owner);
      list.splice(list.map(function (r) { return r.key; }).indexOf(del.dataset.ruleDelete), 1);
      renderSeasons();
      changed();
      return;
    }
    if (t.closest('[data-fare-add-season]')) {
      var s = { key: newKey(), id: null, name: '', start_date: '', end_date: '',
                base: readPrice($('[data-fare-price-host="base"]')), rules: [] };
      state.seasons.push(s);
      openSeason = s.key;
      renderSeasons();
      $('[data-season="' + s.key + '"] [data-season-field="name"]').focus();
      changed();
      return;
    }
    var toggle = t.closest('[data-season-toggle]');
    if (toggle) {
      var key = toggle.closest('[data-season]').dataset.season;
      openSeason = openSeason === key ? null : key;
      renderSeasons();
      return;
    }
    var dropSeason = t.closest('[data-season-delete]');
    if (dropSeason) {
      var gone = season(dropSeason.closest('[data-season]').dataset.season);
      var count = gone.rules.length;
      Crud.confirmThen({
        title: 'Delete ' + (gone.name || 'this season') + '?', subtitle: 'Season',
        body: (count ? 'Its ' + count + ' special price' + (count === 1 ? ' goes' : 's go') + ' with it. ' : '') +
              'Nothing is saved until you save the fare.',
        action: 'Delete'
      }, function () {
        state.seasons = state.seasons.filter(function (x) { return x !== gone; });
        if (openSeason === gone.key) openSeason = null;
        renderSeasons();
        changed();
      });
      return;
    }
    var saveButton = t.closest('[data-fare-save]');
    if (saveButton) { save(saveButton); return; }
    if (t.closest('[data-fare-test-open]')) { openTest(); return; }
    var cancel = t.closest('[data-fare-cancel]');
    if (cancel && dirty) {
      e.preventDefault();
      Crud.confirmThen({ title: 'Discard your changes?', subtitle: 'Fare',
                         body: 'Nothing you changed on this page has been saved.', action: 'Discard' },
        function () { dirty = false; window.location.href = cancel.href; });
    }
  });

  root.addEventListener('input', function (e) {
    var t = e.target;
    var box = t.closest('[data-season]');
    if (box && t.dataset.seasonField) {
      season(box.dataset.season)[t.dataset.seasonField] = t.value;
      refreshSeasonHead(season(box.dataset.season));
    } else if (box && t.dataset.price) {
      season(box.dataset.season).base[t.dataset.price] = t.value.trim();
      refreshSeasonHead(season(box.dataset.season));
    }
    changed();
  });

  root.addEventListener('change', function (e) {
    var t = e.target;
    if (t === company || t === category) applyFilters();
    if (t === vehicleType || t === company || t === category) showTax();
    if (t.dataset.fareField === 'level') showLevel();
    if (t !== category) changed();
  });

  [validFrom, validTo].forEach(function (el) { picker(el, changed); });
  ruleKind.addEventListener('change', showKind);
  ruleBox.querySelector('[data-rule-save]').addEventListener('click', saveRule);
  picker(testDate, runTest);
  testTime.addEventListener('change', runTest);

  window.addEventListener('beforeunload', function (e) {
    if (dirty) { e.preventDefault(); e.returnValue = ''; }
  });

  // -- Start ---------------------------------------------------------------------------

  fillStatic();
  renderSeasons();
  renderBanner();
  if (!canSave) {
    $$('input, select', root).forEach(function (el) { el.disabled = true; });
    $$('input, select', ruleBox).forEach(function (el) { el.disabled = true; });
  }
  dirty = false;
})();

/* Card Discount add/edit page (apps/discount/templates/discount/card_discount_form.html).

   The browser keeps the form tidy -- the grade list follows the card type, a
   day's % unlocks when its day is ticked, "All Days" locks the day rows, only
   the chosen usage type's count is editable -- and posts it whole. The server
   (apps/discount/services.py) checks everything again. */
(function () {
  'use strict';

  var root = document.querySelector('[data-card-discount]');
  if (!root) return;

  var Crud = window.BykyCrud;
  var canSave = root.dataset.canSave === '1';
  var initial = JSON.parse(document.getElementById('cd-initial').textContent);
  var errors = [];
  var dirty = false;

  function $(selector) { return root.querySelector(selector); }
  function $$(selector) { return [].slice.call(root.querySelectorAll(selector)); }
  function esc(text) {
    return String(text == null ? '' : text).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  var company = $('[data-cd-field="company"]');
  var cardType = $('[data-cd-field="card_type"]');
  var grade = $('[data-cd-field="card_grade"]');
  var status = $('[data-cd-field="is_active"]');
  var validFrom = $('[data-cd-date="valid_from"]');
  var validTo = $('[data-cd-date="valid_to"]');
  var all = $('[data-cd-all]');
  var allPercent = $('[data-cd-all-percent]');

  // -- Dates: shown d M Y, sent ISO -------------------------------------------------

  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function toIso(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }
  function picker(el) {
    if (typeof flatpickr === 'undefined') return;
    if (!el._flatpickr) flatpickr(el, { dateFormat: 'd M Y', allowInput: true });
    el._flatpickr.config.onChange.push(changed);
  }
  function isoOf(el) {
    var fp = el._flatpickr;
    if (fp) return fp.selectedDates.length ? toIso(fp.selectedDates[0]) : '';
    return el.value.trim();
  }
  function setIso(el, iso) {
    if (el._flatpickr) { if (iso) el._flatpickr.setDate(iso, false, 'Y-m-d'); else el._flatpickr.clear(false); }
    else el.value = iso || '';
  }

  // -- Card type filters the grades (and company filters the types) ---------------

  function filterOptions() {
    var companyId = company ? company.value : '';
    [].forEach.call(cardType.options, function (o) {
      if (o.value) o.hidden = !!companyId && o.dataset.company !== companyId;
    });
    if (cardType.selectedOptions[0] && cardType.selectedOptions[0].hidden) cardType.value = '';
    var typeId = cardType.value;
    [].forEach.call(grade.options, function (o) {
      if (o.value) o.hidden = !typeId || o.dataset.type !== typeId;
    });
    if (grade.selectedOptions[0] && grade.selectedOptions[0].hidden) grade.value = '';
  }

  // -- Days ------------------------------------------------------------------------

  function syncDays() {
    var allOn = all.checked;
    allPercent.disabled = !allOn;
    $$('[data-cd-day]').forEach(function (box) {
      var input = $('[data-cd-day-percent="' + box.dataset.cdDay + '"]');
      if (allOn) box.checked = true;
      box.disabled = allOn;
      input.disabled = allOn || !box.checked;
    });
  }

  // -- Usage -----------------------------------------------------------------------

  function syncUsage() {
    var chosen = $('[data-cd-usage]:checked');
    $$('[data-cd-usage-limit]').forEach(function (input) {
      input.disabled = !chosen || input.dataset.cdUsageLimit !== chosen.value;
    });
  }

  // -- Collect / fill ----------------------------------------------------------------

  function collect() {
    var usage = $('[data-cd-usage]:checked');
    var limit = usage ? $('[data-cd-usage-limit="' + usage.value + '"]') : null;
    var promotion = $('[data-cd-promotion]:checked');
    return {
      pk: initial.pk,
      company: company ? company.value : initial.company,
      card_type: cardType.value,
      card_grade: grade.value,
      valid_from: isoOf(validFrom),
      valid_to: isoOf(validTo),
      all_days: all.checked,
      all_days_percent: allPercent.value,
      days: all.checked ? [] : $$('[data-cd-day]').filter(function (b) { return b.checked; }).map(function (b) {
        return { weekday: +b.dataset.cdDay, percent: $('[data-cd-day-percent="' + b.dataset.cdDay + '"]').value };
      }),
      promotion: promotion ? promotion.value : '',
      usage_type: usage ? usage.value : '',
      usage_limit: limit ? limit.value : null,
      is_active: status.value === '1'
    };
  }

  function fill() {
    if (company && initial.company) company.value = initial.company;
    filterOptions();
    if (initial.card_type) cardType.value = initial.card_type;
    filterOptions();
    if (initial.card_grade) grade.value = initial.card_grade;
    setIso(validFrom, initial.valid_from);
    setIso(validTo, initial.valid_to);
    status.value = initial.is_active ? '1' : '0';
    all.checked = !!initial.all_days;
    allPercent.value = initial.all_days_percent || '';
    (initial.days || []).forEach(function (d) {
      var box = $('[data-cd-day="' + d.weekday + '"]');
      if (box) { box.checked = true; $('[data-cd-day-percent="' + d.weekday + '"]').value = d.percent; }
    });
    var promotion = $('[data-cd-promotion][value="' + initial.promotion + '"]');
    if (promotion) promotion.checked = true;
    var usage = $('[data-cd-usage][value="' + initial.usage_type + '"]');
    if (usage) usage.checked = true;
    var limit = $('[data-cd-usage-limit="' + initial.usage_type + '"]');
    if (limit && initial.usage_limit) limit.value = initial.usage_limit;
    syncDays();
    syncUsage();
  }

  // -- Banner and save ---------------------------------------------------------------

  function renderBanner() {
    var host = document.querySelector('[data-cd-banner]');
    if (!errors.length) { host.innerHTML = ''; return; }
    host.innerHTML = '<ul class="scr-fare-banner is-error"><li class="scr-fare-banner-title">' +
      (errors.length === 1 ? 'Not saved' : errors.length + ' things to fix') + '</li>' +
      errors.map(function (e) {
        return '<li>' + (e.field ? '<span class="scr-msg-field">' + esc(e.field) + '</span> ' : '') + esc(e.message) + '</li>';
      }).join('') + '</ul>';
  }

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
      errors = body.errors || [{ field: '', message: 'Something went wrong.' }];
      renderBanner();
      Crud.showMessages({ title: result.status === 403 ? 'Not allowed' : 'Please fix the following', items: errors });
    });
  }

  function changed() { dirty = true; }

  // -- Events ------------------------------------------------------------------------

  root.addEventListener('change', function (e) {
    var t = e.target;
    if (t === company || t === cardType) filterOptions();
    if (t === all && !all.checked) {
      // Leaving "All Days": start from no days, not seven ticked and empty.
      $$('[data-cd-day]').forEach(function (box) { box.checked = false; });
      $$('[data-cd-day-percent]').forEach(function (input) { input.value = ''; });
    }
    if (t === all || t.hasAttribute('data-cd-day')) syncDays();
    if (t.hasAttribute('data-cd-usage')) syncUsage();
    changed();
  });
  root.addEventListener('input', changed);

  root.addEventListener('click', function (e) {
    var saveButton = e.target.closest('[data-cd-save]');
    if (saveButton) { save(saveButton); return; }
    var cancel = e.target.closest('[data-cd-cancel]');
    if (cancel && dirty) {
      e.preventDefault();
      Crud.confirmThen({ title: 'Discard your changes?', subtitle: 'Card Discount',
                         body: 'Your changes are not saved.', action: 'Discard' },
        function () { dirty = false; window.location.href = cancel.href; });
    }
  });

  window.addEventListener('beforeunload', function (e) {
    if (dirty) { e.preventDefault(); e.returnValue = ''; }
  });

  [validFrom, validTo].forEach(picker);
  fill();
  if (!canSave) $$('input, select').forEach(function (el) { el.disabled = true; });
  dirty = false;
})();

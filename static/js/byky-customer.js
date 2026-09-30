/* Customer drawer: Full Number is read-only and built by the server from
   Country Code + Phone No (digits only -- apps/rental/models.py::full_number).
   This previews it as those two are typed, so the person sees the number the
   customer will be found by. Also refreshed when the drawer opens on a record. */
(function () {
  'use strict';

  function digits(value) { return (value || '').replace(/\D/g, ''); }

  function refresh() {
    var code = document.getElementById('mobile_country_code');
    var number = document.getElementById('mobile_no');
    var full = document.getElementById('mobile_full');
    if (!code || !number || !full) return;
    full.value = digits(code.value) + digits(number.value);
  }

  document.addEventListener('input', function (event) {
    if (event.target && (event.target.id === 'mobile_country_code' || event.target.id === 'mobile_no')) refresh();
  });
  // byky-drawer.js fills the fields as the drawer opens (Edit: the saved
  // number; Add: empty); the preview follows once it is shown.
  document.addEventListener('shown.bs.offcanvas', refresh);
})();

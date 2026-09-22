  // Progressive enhancement only — the form works without it.
  var t = document.getElementById('pw-toggle'), p = document.getElementById('password');
  if (t && p) {
    t.hidden = false;
    t.addEventListener('click', function () {
      var shown = p.type === 'text';
      p.type = shown ? 'password' : 'text';
      t.textContent = shown ? 'Show' : 'Hide';
    });
  }

  // Spinner while the sign-in POST is in flight. A plain form submit reloads
  // the page either way -- success navigates to the dashboard, failure
  // re-renders this same template fresh from the server -- so there is no
  // "stop the spinner on error" to wire up separately: a failed login is a
  // brand-new page load, is-loading was never in that HTML to begin with.
  var form = document.querySelector('form'), submitBtn = document.getElementById('signin-submit');
  if (form && submitBtn) {
    form.addEventListener('submit', function () {
      submitBtn.classList.add('is-loading');
      submitBtn.disabled = true;
    });
  }

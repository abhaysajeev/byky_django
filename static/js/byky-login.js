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

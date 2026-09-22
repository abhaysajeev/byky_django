/* Keeps --byky-sb-w (consumed by sidebar-integration.css on .layout-page) in sync
   with the sidebar's collapsed/expanded state and live drag-resize width.
   Deliberately keyed off the .is-collapsed class rather than the sidebar's raw
   rendered width, so that hover-to-peek -- which visually grows the icon rail
   without pushing content -- never touches the content's padding. */
(function () {
  var sidebar = document.getElementById('byky-sidebar');
  var root = document.documentElement;
  if (!sidebar) return;
  var COLLAPSED_W = '76px';

  function sync() {
    var collapsed = sidebar.classList.contains('is-collapsed');
    var w = collapsed
      ? COLLAPSED_W
      : (getComputedStyle(sidebar).getPropertyValue('--sb-w').trim() || sidebar.getBoundingClientRect().width + 'px');
    root.style.setProperty('--byky-sb-w', w);
  }

  sync();
  new MutationObserver(sync).observe(sidebar, { attributes: true, attributeFilter: ['class', 'style'] });

  /* Every sub-item click is a real navigation (full page load), not a SPA
     transition -- sidebar.js has no memory of scroll position across that, so
     the nav snaps back to the top on every click and visibly "jumps" away from
     wherever the user just was. Persist and restore it across loads. */
  var nav = sidebar.querySelector('.byky-nav');
  if (nav) {
    var SCROLL_KEY = 'byky.sidebar.scrollTop';
    var saved = sessionStorage.getItem(SCROLL_KEY);
    if (saved !== null) {
      var restore = function () { nav.scrollTop = parseInt(saved, 10) || 0; };
      requestAnimationFrame(function () { requestAnimationFrame(restore); });
    }
    nav.addEventListener('scroll', function () {
      try { sessionStorage.setItem(SCROLL_KEY, String(nav.scrollTop)); } catch (e) {}
    }, { passive: true });
  }
})();

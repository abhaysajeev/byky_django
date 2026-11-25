/* Zoom inside an image viewer (client, 24 Nov 2026).
 *
 * Any [data-img-zoom] box holding one <img> gets a small − · 100% · + bar.
 * Zooming makes the image larger than the box; the box scrolls, and a zoomed
 * image can also be dragged (mouse or finger). The % resets it. Every new
 * image, and every time the viewer opens, starts back at 100%.
 *
 * Used by the card photo, service request photo, image sharing, asset check
 * and receipt logo viewers. byky-image-share.js / byky-device-settings.js
 * call window.BykyImageZoom.reset(box) when they swap the picture.
 */
(function () {
  'use strict';

  var STEPS = [1, 1.5, 2, 3, 4];

  function setup(box) {
    if (box.dataset.imgZoomReady) return;
    box.dataset.imgZoomReady = '1';
    var img = box.querySelector('img');
    if (!img) return;
    var level = 0, fitWidth = 0;

    var bar = document.createElement('div');
    bar.className = 'scr-img-zoom-bar';
    bar.innerHTML =
      '<button type="button" class="scr-icon-btn" data-zoom="out" aria-label="Zoom out" title="Zoom out">' +
      '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M5 12h14"></path></svg></button>' +
      '<button type="button" class="scr-img-zoom-level" data-zoom="reset" aria-label="Reset zoom" title="Reset to 100%">100%</button>' +
      '<button type="button" class="scr-icon-btn" data-zoom="in" aria-label="Zoom in" title="Zoom in">' +
      '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14"></path></svg></button>';
    // The buttons sit on a frame around the scrolling box, not inside it, so
    // scrolling or dragging a zoomed picture never moves them.
    var frame = document.createElement('div');
    frame.className = 'scr-img-zoom-frame';
    box.parentNode.insertBefore(frame, box);
    frame.appendChild(box);
    frame.appendChild(bar);
    var label = bar.querySelector('[data-zoom="reset"]');
    var zoomOut = bar.querySelector('[data-zoom="out"]');
    var zoomIn = bar.querySelector('[data-zoom="in"]');

    function apply(next) {
      var scale = STEPS[next];
      if (next === 0) {
        img.style.width = img.style.maxWidth = img.style.maxHeight = '';
        box.classList.remove('is-zoomed');
      } else {
        if (level === 0) fitWidth = img.getBoundingClientRect().width || img.naturalWidth || 400;
        // Keep the middle of what is in view in the middle after zooming.
        var ratioX = (box.scrollLeft + box.clientWidth / 2) / Math.max(box.scrollWidth, 1);
        var ratioY = (box.scrollTop + box.clientHeight / 2) / Math.max(box.scrollHeight, 1);
        img.style.maxWidth = 'none';
        img.style.maxHeight = 'none';
        img.style.width = Math.round(fitWidth * scale) + 'px';
        box.classList.add('is-zoomed');
        box.scrollLeft = ratioX * box.scrollWidth - box.clientWidth / 2;
        box.scrollTop = ratioY * box.scrollHeight - box.clientHeight / 2;
      }
      level = next;
      label.textContent = Math.round(scale * 100) + '%';
      zoomOut.disabled = level === 0;
      zoomIn.disabled = level === STEPS.length - 1;
    }

    bar.addEventListener('click', function (event) {
      var btn = event.target.closest('[data-zoom]');
      if (!btn || btn.disabled) return;
      if (btn.dataset.zoom === 'in') apply(Math.min(level + 1, STEPS.length - 1));
      else if (btn.dataset.zoom === 'out') apply(Math.max(level - 1, 0));
      else apply(0);
    });

    // Drag a zoomed image to look around.
    var drag = null;
    box.addEventListener('pointerdown', function (event) {
      if (level === 0 || event.target.closest('.scr-img-zoom-bar')) return;
      drag = { x: event.clientX, y: event.clientY, left: box.scrollLeft, top: box.scrollTop };
      box.classList.add('is-dragging');
      if (box.setPointerCapture) box.setPointerCapture(event.pointerId);
      event.preventDefault();
    });
    box.addEventListener('pointermove', function (event) {
      if (!drag) return;
      box.scrollLeft = drag.left - (event.clientX - drag.x);
      box.scrollTop = drag.top - (event.clientY - drag.y);
    });
    function stop() { drag = null; box.classList.remove('is-dragging'); }
    box.addEventListener('pointerup', stop);
    box.addEventListener('pointercancel', stop);
    img.addEventListener('dragstart', function (event) { event.preventDefault(); });

    // While a new picture downloads: a spinner, never the previous picture.
    var wait = document.createElement('span');
    wait.className = 'scr-img-zoom-wait';
    wait.setAttribute('aria-hidden', 'true');
    var failed = document.createElement('p');
    failed.className = 'scr-img-zoom-error';
    failed.textContent = 'The image could not be loaded.';
    failed.hidden = true;
    box.appendChild(wait);
    box.appendChild(failed);

    box.bykyZoomReset = function () { apply(0); };
    img.addEventListener('load', function () {
      box.classList.remove('is-loading');
      box.bykyZoomReset();
    });
    img.addEventListener('error', function () {
      if (!img.getAttribute('src')) return;
      box.classList.remove('is-loading');
      failed.hidden = false;
    });
    box.bykyLoad = function (url) {
      failed.hidden = true;
      img.removeAttribute('src');          // the old picture goes at once
      box.classList.add('is-loading');
      apply(0);
      img.src = url;                       // a later call replaces it; the old load is dropped
    };

    // Back to 100% each time the viewer opens.
    var modal = box.closest('.scr-modal');
    if (modal && window.MutationObserver) {
      new MutationObserver(function () { if (!modal.hidden) box.bykyZoomReset(); })
        .observe(modal, { attributes: true, attributeFilter: ['hidden'] });
    }
    apply(0);
  }

  function setupAll(root) {
    (root || document).querySelectorAll('[data-img-zoom]').forEach(setup);
  }

  window.BykyImageZoom = {
    setup: setupAll,
    reset: function (box) { if (box && box.bykyZoomReset) box.bykyZoomReset(); },
    // Show `url` in the viewer holding `img`: the previous picture is cleared
    // first and a spinner shows until the new one has arrived (the image
    // sharing flash, client 24 Nov 2026).
    load: function (img, url) {
      var box = img.closest('[data-img-zoom]');
      if (box && !box.bykyLoad) setup(box);
      if (box && box.bykyLoad) box.bykyLoad(url);
      else { img.removeAttribute('src'); img.src = url; }
    },
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { setupAll(); });
  else setupAll();
})();

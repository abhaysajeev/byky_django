/**
 * BYKY navbar alerts.
 *
 * An alert arrives, a tone sounds, a strip appears beside the bell with the
 * title and one line of detail, and the bell's badge counts what is unread.
 * Opening the bell shows the full list; a row opens Missing & Alerts.
 *
 * The source is Antenna Tracking (design 07 section 7.1). What a user may see
 * and has read is decided by the server; a user without the privilege gets no
 * configuration and this script stops at once.
 */

'use strict';

(function () {
  var STRIP_MS = 9000;      // how long the strip stays up
  var MAX_ROWS = 12;        // most recent alerts kept in the dropdown
  var MUTE_KEY = 'byky-alerts-muted';

  var node = document.getElementById('byky-alert-feed');
  var strip = document.getElementById('byky-alert-strip');
  var badge = document.getElementById('byky-bell-badge');
  var list = document.getElementById('byky-alert-list');
  var empty = document.getElementById('byky-alert-empty');
  var clearBtn = document.getElementById('byky-alert-clear');
  if (!node || !strip || !badge || !list) return;

  // The real source: Antenna Tracking's feed, present only for a user who may
  // read Missing & Alerts (design 07 section 7.1).
  var config = null;
  var configNode = document.getElementById('byky-tracking-bell');
  try {
    config = configNode ? JSON.parse(configNode.textContent) : null;
  } catch (e) {
    config = null;
  }
  if (!config) return;

  var unread = 0;
  var stripTimer = null;

  /* ── sound ────────────────────────────────────────────────────────
     A short two-tone chime built with the Web Audio API rather than an
     audio asset -- no file to ship, no CDN, and the pitch can carry the
     severity. Browsers refuse to start audio before the user has
     interacted with the page, so the context is created lazily and
     resumed on the first gesture; until then alerts arrive silently
     rather than throwing. */
  var Ctx = window.AudioContext || window.webkitAudioContext;
  var audio = null;
  var muted = false;
  try {
    muted = localStorage.getItem(MUTE_KEY) === '1';
  } catch (e) {
    muted = false; // private mode / blocked storage: default to audible
  }

  /* Resume on *every* gesture, not just the first. A single `once` attempt is
     fragile: resume() is async and can fail or be ignored (a tab that is
     still loading, no output device yet), and once that one listener had
     fired there was nothing left to retry it -- alerts then stayed silent
     for the rest of the session even though the user had clicked. Listeners
     are passive and cheap, and resume() is a no-op once running. */
  function unlock() {
    if (!Ctx) return;
    if (!audio) audio = new Ctx();
    if (audio.state === 'suspended') audio.resume();
  }
  ['click', 'keydown', 'touchstart'].forEach(function (evt) {
    document.addEventListener(evt, unlock, { passive: true });
  });

  function ping(severity) {
    if (muted || !Ctx) return;
    if (!audio) audio = new Ctx();
    // Nudge it awake for next time, then bail rather than queueing tones that
    // would all fire at once whenever the context finally starts.
    if (audio.state !== 'running') {
      audio.resume();
      return;
    }

    // Critical is a lower, more insistent pair; warning/info sit higher.
    // A triangle wave carries further than a sine at the same gain without
    // getting harsh, which is what makes this audible over a room.
    var pair = severity === 'critical' ? [660, 495] : [880, 1174];
    var peak = severity === 'critical' ? 0.5 : 0.36;
    var master = audio.createGain();
    master.gain.value = 0.9;
    master.connect(audio.destination);

    pair.forEach(function (freq, i) {
      var osc = audio.createOscillator();
      var gain = audio.createGain();
      var at = audio.currentTime + i * 0.14;
      osc.type = 'triangle';
      osc.frequency.setValueAtTime(freq, at);
      gain.gain.setValueAtTime(0.0001, at);
      gain.gain.exponentialRampToValueAtTime(peak, at + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, at + 0.30);
      osc.connect(gain).connect(master);
      osc.start(at);
      osc.stop(at + 0.32);
    });
  }

  /* ── mute toggle (remembered per browser) ────────────────────────── */
  var muteBtn = document.getElementById('byky-alert-mute');

  function applyMute() {
    if (!muteBtn) return;
    muteBtn.setAttribute('aria-pressed', muted ? 'true' : 'false');
    muteBtn.title = muted ? 'Unmute alert sound' : 'Mute alert sound';
  }

  if (muteBtn) {
    applyMute();
    muteBtn.addEventListener('click', function (e) {
      e.stopPropagation(); // keep the dropdown open
      muted = !muted;
      try {
        localStorage.setItem(MUTE_KEY, muted ? '1' : '0');
      } catch (err) {
        /* storage blocked -- the toggle still works for this page */
      }
      applyMute();
      if (!muted) {
        unlock();
        ping('info'); // confirm audio is back, and that it is audible
      }
    });
  }

  /* ── the strip beside the bell ───────────────────────────────────── */
  function showStrip(alert) {
    strip.className = 'byky-alert-strip is-' + alert.severity;
    strip.href = alert.url;
    strip.querySelector('.byky-alert-title').textContent = alert.title;
    strip.querySelector('.byky-alert-detail').textContent = alert.subject;
    strip.hidden = false;
    // restart the entry animation even if a strip is already showing
    strip.classList.remove('is-in');
    void strip.offsetWidth;
    strip.classList.add('is-in');

    clearTimeout(stripTimer);
    stripTimer = setTimeout(hideStrip, STRIP_MS);
  }

  function hideStrip() {
    clearTimeout(stripTimer);
    strip.hidden = true;
    strip.classList.remove('is-in');
  }

  strip.querySelector('.byky-alert-x').addEventListener('click', function (e) {
    e.preventDefault();
    e.stopPropagation();
    hideStrip();
  });

  /* ── the dropdown list ───────────────────────────────────────────── */
  function timeLabel(date) {
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  function addRow(alert) {
    if (empty) empty.hidden = true;
    var known = list.querySelector('[data-alert-id="' + alert.id + '"]');
    if (known) {
      known.classList.toggle('is-read', !!alert.is_read);
      return;
    }

    var row = document.createElement('a');
    row.className = 'byky-alert-row is-' + alert.severity;
    row.href = alert.url;
    row.dataset.alertId = alert.id;
    if (alert.is_read) row.classList.add('is-read');

    var dot = document.createElement('span');
    dot.className = 'byky-alert-row-dot';
    row.appendChild(dot);

    var body = document.createElement('span');
    body.className = 'byky-alert-row-body';

    var head = document.createElement('span');
    head.className = 'byky-alert-row-head';
    var title = document.createElement('b');
    title.textContent = alert.title;
    var when = document.createElement('time');
    when.textContent = timeLabel(new Date(alert.at));
    head.appendChild(title);
    head.appendChild(when);

    var detail = document.createElement('span');
    detail.className = 'byky-alert-row-detail';
    detail.textContent = alert.detail;

    body.appendChild(head);
    body.appendChild(detail);
    row.appendChild(body);

    // newest first, whichever order they arrive in
    var after = Array.prototype.find.call(list.querySelectorAll('.byky-alert-row'), function (r) {
      return Number(r.dataset.alertId) < alert.id;
    });
    list.insertBefore(row, after || null);

    var rows = list.querySelectorAll('.byky-alert-row');
    for (var i = MAX_ROWS; i < rows.length; i++) rows[i].remove();
  }

  function setUnread(n) {
    unread = n;
    badge.textContent = n > 9 ? '9+' : String(n);
    badge.hidden = n === 0;
    document.querySelector('.byky-bell').classList.toggle('has-unread', n > 0);
  }

  function csrf() {
    var match = document.cookie.match(new RegExp('(?:^|; )' + (config.csrf_cookie || 'csrftoken') + '=([^;]*)'));
    return match ? decodeURIComponent(match[1]) : '';
  }

  if (clearBtn) {
    clearBtn.addEventListener('click', function () {
      setUnread(0);
      list.querySelectorAll('.byky-alert-row').forEach(function (r) {
        r.classList.add('is-read');
      });
      fetch(config.read_url, { method: 'POST', headers: { 'X-CSRFToken': csrf() }, credentials: 'same-origin' });
    });
  }

  /* -- the real feed: asked for on load, on a socket's "look again", and every
     60 s in case the socket is down. The first answer never rings. ---------- */
  var newest = null;
  var asking = false;

  function ask() {
    if (asking) return;
    asking = true;
    fetch(config.feed_url, { credentials: 'same-origin' }).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (data) {
      asking = false;
      if (!data) return;
      data.items.slice().reverse().forEach(addRow);
      setUnread(data.unread);
      var top = data.items.length ? data.items[0] : null;
      if (top && newest !== null && top.id > newest && !top.is_read) {
        try { ping(top.severity); } catch (e) { /* the alert still shows */ }
        showStrip(top);
      }
      newest = top ? Math.max(top.id, newest || 0) : (newest || 0);
    }).catch(function () { asking = false; });
  }

  function listen(delay) {
    try {
      var socket = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/tracking/');
      socket.onmessage = function (e) {
        var message = JSON.parse(e.data);
        if (message.alert) ask();
      };
      socket.onopen = function () { delay = 2000; };
      socket.onclose = function () { setTimeout(function () { listen(Math.min(delay * 2, 60000)); }, delay); };
    } catch (e) { /* the 60 s poll carries on */ }
  }

  setUnread(0);
  ask();
  listen(2000);
  setInterval(ask, 60000);
})();

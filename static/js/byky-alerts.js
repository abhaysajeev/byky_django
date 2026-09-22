/**
 * BYKY navbar alerts.
 *
 * Demonstrates the alerting behaviour the FSD specifies (3.19 Inventory System
 * Alerts, 10.2 System Security Alert Dispatch): an alert arrives, a tone
 * sounds, a strip appears beside the bell with the title and one line of
 * detail, and the bell's badge counts what is unread. Opening the bell shows
 * the full list; any row opens the FSD screen that owns that alert type.
 *
 * The wireframe has no live telemetry, so arrivals are replayed from a
 * server-rendered pool (#byky-alert-feed, built by apps/byky_core/alerts.py)
 * on a fixed interval. Alert types and severities are the FSD's own; the
 * stations and vehicles they name are real client records.
 *
 * Nothing here persists: a reload starts the run again.
 */

'use strict';

(function () {
  var INTERVAL_MS = 165000; // one alert every 2m 45s
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

  var feed = [];
  try {
    feed = JSON.parse(node.textContent) || [];
  } catch (e) {
    return;
  }
  if (!feed.length) return;

  var unread = 0;
  var cursor = 0;
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

    var row = document.createElement('a');
    row.className = 'byky-alert-row is-' + alert.severity;
    row.href = alert.url;

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
    when.textContent = timeLabel(new Date());
    head.appendChild(title);
    head.appendChild(when);

    var detail = document.createElement('span');
    detail.className = 'byky-alert-row-detail';
    detail.textContent = alert.detail;

    body.appendChild(head);
    body.appendChild(detail);
    row.appendChild(body);

    list.insertBefore(row, list.firstChild);

    var rows = list.querySelectorAll('.byky-alert-row');
    for (var i = MAX_ROWS; i < rows.length; i++) rows[i].remove();
  }

  function setUnread(n) {
    unread = n;
    badge.textContent = n > 9 ? '9+' : String(n);
    badge.hidden = n === 0;
    document.querySelector('.byky-bell').classList.toggle('has-unread', n > 0);
  }

  if (clearBtn) {
    clearBtn.addEventListener('click', function () {
      setUnread(0);
      list.querySelectorAll('.byky-alert-row').forEach(function (r) {
        r.classList.add('is-read');
      });
    });
  }

  // Opening the bell is acknowledgement enough to stop the badge nagging.
  var bell = document.querySelector('.byky-bell');
  if (bell) bell.addEventListener('click', function () { setUnread(0); });

  /* ── the replay loop ─────────────────────────────────────────────── */
  function raise() {
    var alert = feed[cursor % feed.length];
    cursor++;

    // Sound first, then the visuals. All of it runs in one synchronous tick,
    // so the two land together; starting the tone before the DOM work just
    // keeps the audio off the far side of a layout pass. Guarded so a failed
    // ping can never cost the notification itself.
    try {
      ping(alert.severity);
    } catch (e) {
      /* audio unavailable -- the alert still shows */
    }

    addRow(alert);
    setUnread(unread + 1);
    showStrip(alert);
  }

  setUnread(0);
  // First alert lands shortly after load so the behaviour is visible without
  // waiting out a full interval.
  setTimeout(raise, 4000);
  setInterval(raise, INTERVAL_MS);
})();

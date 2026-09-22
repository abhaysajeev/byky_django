/* byky-device-approval.js -- the Device Approval actions.
 *
 * Approve, Reconnect, Reject, Block, Unblock -- and the Pending tab's
 * registration-number lookup.
 * Each posts to /devices/approval/<pk>/<action>/; the rules are server-side in
 * apps/devices/services.py, which re-checks the device's status under a row
 * lock, so this file only collects input and reports the answer.
 *
 * The action buttons live in row menus that byky-screen.js moves out of the
 * table, so each button carries what it needs as data-* attributes and clicks
 * are caught at the document.
 */
(function () {
  'use strict';

  var root = document.querySelector('[data-device-approval]');
  if (!root) return;

  function crud() { return window.BykyCrud || {}; }

  function urlFor(action, pk) {
    return root.dataset['url' + action.charAt(0).toUpperCase() + action.slice(1)]
      .replace(/\/0\/([a-z]+)\/$/, '/' + pk + '/$1/');
  }

  function openModal(name) {
    var box = document.querySelector('[data-scr-modal="' + name + '"]');
    var veil = document.querySelector('[data-scr-modal-veil="' + name + '"]');
    if (box) box.hidden = false;
    if (veil) veil.hidden = false;
    return box;
  }

  function closeModal(name) {
    var box = document.querySelector('[data-scr-modal="' + name + '"]');
    var veil = document.querySelector('[data-scr-modal-veil="' + name + '"]');
    if (box) box.hidden = true;
    if (veil) veil.hidden = true;
  }

  /* Post once: the button stays disabled until the server answers. */
  function send(button, action, pk, payload, modalName) {
    if (button.disabled) return;
    button.disabled = true;
    var post = crud().post;
    if (!post) { button.disabled = false; return; }
    post(urlFor(action, pk), payload).then(function (res) {
      var body = res.body || {};
      if (body.ok) {
        closeModal(modalName);
        if (crud().toastAfterReload) crud().toastAfterReload(body.message || 'Done.');
        window.location.reload();
        return;
      }
      button.disabled = false;
      if (crud().showMessages) {
        crud().showMessages({ title: 'Could not complete', items: body.errors || [{ field: '', message: 'Something went wrong.' }] });
      }
    }).catch(function () {
      button.disabled = false;
      if (crud().showMessages) {
        crud().showMessages({ title: 'Could not complete', items: [{ field: '', message: 'Could not reach the server. Try again.' }] });
      }
    });
  }

  function label(d) { return d.name ? d.name + ' (' + d.number + ')' : 'device ' + d.number; }

  /* ── Approve ──────────────────────────────────────────────────────── */

  var approve = document.querySelector('[data-scr-modal="device-approve"]');
  var nameInput = approve && approve.querySelector('[data-approve-name]');
  var station = approve && approve.querySelector('[data-approve-station]');
  var approveGo = approve && approve.querySelector('[data-approve-go]');
  var approving = null;

  function openApprove(d) {
    if (!approve) return;
    approving = d;
    approve.querySelector('[data-approve-title]').textContent = 'Approve device ' + d.number;
    approve.querySelector('[data-approve-sub]').textContent =
      (d.model ? d.model + ' \u00b7 ' : '') + 'Give it a name staff will recognise.';
    nameInput.value = d.name || '';
    station.value = '';
    approveGo.disabled = false;
    openModal('device-approve');
    setTimeout(function () { nameInput.focus(); }, 50);
  }

  if (approveGo) approveGo.addEventListener('click', function () {
    if (!approving) return;
    send(approveGo, 'approve', approving.pk, {
      name: nameInput.value.trim(),
      branch: station.value || null
    }, 'device-approve');
  });

  /* ── Reconnect / Reject / Block / Unblock ─────────────────────────── */

  var box = document.querySelector('[data-scr-modal="device-action"]');
  var go = box && box.querySelector('[data-action-go]');
  var alt = box && box.querySelector('[data-action-alt]');
  var reasonField = box && box.querySelector('[data-action-reason-field]');
  var reason = box && box.querySelector('[data-action-reason]');
  var current = null;

  var COPY = {
    reconnect: function (d) {
      var match = d.matchName ? d.matchName + ' (' + d.matchNumber + ')' : 'device ' + d.matchNumber;
      return {
        title: 'Reconnect after reinstall',
        sub: 'Request ' + d.number + ' looks like ' + match + '.',
        line: 'Confirm only if this is the same tablet. It keeps number ' + d.matchNumber +
              ', its name, station and history; request ' + d.number + ' is discarded and anyone ' +
              'logged in on the old install is signed out.',
        go: 'Reconnect', danger: false, reason: false, alt: true
      };
    },
    reject: function (d) {
      return {
        title: 'Reject ' + label(d) + '?', sub: 'The registration is retired.',
        line: 'The tablet will be told to register again. Use this for a registration that should not exist.',
        go: 'Reject', danger: true, reason: true, alt: false
      };
    },
    block: function (d) {
      return {
        title: 'Block ' + label(d) + '?', sub: 'Nobody can log in on it.',
        line: 'Anyone logged in on it is signed out now. Its station stays recorded, so unblocking puts it back.',
        go: 'Block', danger: true, reason: true, alt: false
      };
    },
    unblock: function (d) {
      return {
        title: 'Unblock ' + label(d) + '?', sub: 'It can be used again.',
        line: 'Staff can log in on it again at its station.',
        go: 'Unblock', danger: false, reason: true, alt: false
      };
    }
  };

  function openAction(action, d) {
    if (!box) return;
    current = { action: action, d: d };
    var copy = COPY[action](d);
    box.querySelector('[data-action-title]').textContent = copy.title;
    box.querySelector('[data-action-sub]').textContent = copy.sub;
    box.querySelector('[data-action-line]').textContent = copy.line;
    reasonField.hidden = !copy.reason;
    reason.value = '';
    alt.hidden = !copy.alt;
    go.textContent = copy.go;
    go.className = copy.danger ? 'scr-btn-danger' : 'scr-btn-primary';
    go.disabled = false;
    openModal('device-action');
  }

  if (go) go.addEventListener('click', function () {
    if (!current) return;
    send(go, current.action, current.d.pk, { reason: reason.value.trim() }, 'device-action');
  });

  if (alt) alt.addEventListener('click', function () {
    if (!current) return;
    var d = current.d;
    closeModal('device-action');
    openApprove(d);                 // "Register as new": a device in its own right
  });

  /* ── Pending: find a request by its registration number ──────────────
     The operator reads the number off the waiting screen over the phone;
     the admin types it here. The list below narrows as they type (the
     shared search in byky-screen.js); an exact number also shows a card
     with its actions, and Enter opens Approve. Built with text nodes: the
     model name comes from the tablet and is never treated as markup. */

  var lookup = root.querySelector('[data-dev-lookup]');
  var card = root.querySelector('[data-dev-match]');

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text) node.textContent = text;
    return node;
  }

  function rowFor(number) {
    return root.querySelector('tr[data-number="' + number + '"]');
  }

  function actionButton(row, action, text, cls) {
    var b = el('button', cls, text);
    b.type = 'button';
    b.dataset.deviceAction = action;
    ['pk', 'number', 'name', 'channel', 'model', 'matchName', 'matchNumber'].forEach(function (key) {
      if (row.dataset[key] !== undefined) b.dataset[key] = row.dataset[key];
    });
    return b;
  }

  function renderCard(number) {
    card.textContent = '';
    if (!number) { card.hidden = true; return null; }
    var row = rowFor(number);
    card.hidden = false;
    card.className = 'scr-dev-match';

    if (!row) {
      card.classList.add('is-none');
      card.appendChild(el('div', 'scr-dev-match-title', 'No request ' + number));
      card.appendChild(el('div', 'scr-dev-match-line',
        'Check the number with the operator. It is on the tablet\u2019s waiting screen.'));
      return null;
    }

    var state = row.dataset.state;
    if (state !== 'pending') {
      card.classList.add('is-done');
      card.appendChild(el('div', 'scr-dev-match-title',
        number + (row.dataset.name ? ' \u00b7 ' + row.dataset.name : '')));
      card.appendChild(el('div', 'scr-dev-match-line',
        state === 'approved' ? 'Already approved \u2014 see the Approved tab.'
                             : 'This device is blocked \u2014 see the Blocked tab.'));
      return null;
    }

    var info = el('div', 'scr-dev-match-info');
    info.appendChild(el('div', 'scr-dev-match-title',
      'Request ' + number + (row.dataset.model ? ' \u00b7 ' + row.dataset.model : '')));
    info.appendChild(el('div', 'scr-dev-match-line',
      [row.dataset.platform, row.dataset.app + ' app', 'registered ' + row.dataset.registered]
        .filter(Boolean).join(' \u00b7 ')));
    if (row.dataset.matchNumber) {
      info.appendChild(el('div', 'scr-dev-match-line is-hint',
        'Looks like ' + (row.dataset.matchName || 'device') + ' (' + row.dataset.matchNumber + ') reinstalling'));
    }
    card.appendChild(info);

    if (card.hasAttribute('data-can-approve')) {
      var actions = el('div', 'scr-dev-match-actions');
      var main = row.dataset.matchNumber
        ? actionButton(row, 'reconnect', 'Reconnect', 'scr-btn-primary')
        : actionButton(row, 'approve', 'Approve', 'scr-btn-primary');
      actions.appendChild(actionButton(row, 'reject', 'Reject', 'scr-btn-danger'));
      actions.appendChild(main);
      card.appendChild(actions);
      return main;
    }
    return null;
  }

  if (lookup && card) {
    var mainAction = null;
    lookup.addEventListener('input', function () {
      var digits = lookup.value.replace(/\D/g, '');
      if (digits !== lookup.value.trim()) lookup.value = digits;   // numbers only
      mainAction = renderCard(digits);
    });
    lookup.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && mainAction) {
        e.preventDefault();
        mainAction.click();
      }
    });
  }

  /* ── the menu buttons ─────────────────────────────────────────────── */

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-device-action]');
    if (!btn) return;
    e.preventDefault();
    var d = {
      pk: btn.dataset.pk, number: btn.dataset.number, name: btn.dataset.name,
      channel: btn.dataset.channel, model: btn.dataset.model,
      matchName: btn.dataset.matchName, matchNumber: btn.dataset.matchNumber
    };
    var menu = btn.closest('.scr-menu');
    if (menu) menu.hidden = true;
    if (btn.dataset.deviceAction === 'approve') openApprove(d);
    else openAction(btn.dataset.deviceAction, d);
  });
})();

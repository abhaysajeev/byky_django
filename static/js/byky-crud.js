/* byky-crud.js -- saving and deleting from a .scr-* screen.
 *
 * The ported drawer and grid were built against static data: the drawer's form
 * never submitted, and Delete removed a row from the page and nothing else.
 * This file is the missing half. It is deliberately a separate file so
 * byky-drawer.js and byky-screen.js stay diffable against the wireframe.
 *
 * What it adds:
 *   - Save / Save & New: collect [data-field], POST JSON, act on the answer
 *   - postFile: the same, as multipart, for a screen that uploads a file
 *   - Delete: confirm, POST, and report when the row is still in use
 *   - The two modals and the toast described in DESIGN.md
 *
 * The drawer keeps its values while the server answers, so a validation error
 * never costs someone their typing.
 */
(function () {
  'use strict';

  /* ── plumbing ─────────────────────────────────────────────────────── */

  function csrf() {
    var input = document.querySelector('[name="csrfmiddlewaretoken"]');
    if (input) return input.value;
    var match = document.cookie.match(/csrftoken=([^;]+)/);
    return match ? match[1] : '';
  }

  function post(url, payload) {
    return fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
      body: JSON.stringify(payload || {})
    }).then(function (response) {
      return response.json()
        .catch(function () { return { ok: false, errors: [{ field: '', message: 'The server did not answer properly.' }] }; })
        .then(function (body) { return { status: response.status, body: body }; });
    });
  }

  /* A file cannot ride inside the JSON a drawer posts, so an upload goes as
     multipart with the same CSRF token and answers in the same shape. */
  function postFile(url, formData) {
    return fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'X-CSRFToken': csrf() },
      body: formData
    }).then(function (response) {
      return response.json()
        .catch(function () { return { ok: false, errors: [{ field: '', message: 'The server did not answer properly.' }] }; })
        .then(function (body) { return { status: response.status, body: body }; });
    });
  }

  /* ── modals ───────────────────────────────────────────────────────── */

  function modal(name) {
    return {
      box: document.querySelector('[data-scr-modal="' + name + '"]'),
      veil: document.querySelector('[data-scr-modal-veil="' + name + '"]')
    };
  }

  function open(name) {
    var parts = modal(name);
    if (!parts.box) return null;
    parts.box.hidden = false;
    if (parts.veil) parts.veil.hidden = false;
    return parts;
  }

  function close(name) {
    var parts = modal(name);
    if (parts.box) parts.box.hidden = true;
    if (parts.veil) parts.veil.hidden = true;
  }

  document.addEventListener('click', function (e) {
    var closer = e.target.closest('[data-scr-modal-close]');
    if (closer) {
      var box = closer.closest('[data-scr-modal]');
      if (box) close(box.dataset.scrModal);
      return;
    }
    var veil = e.target.closest('[data-scr-modal-veil]');
    if (veil) close(veil.dataset.scrModalVeil);
  });

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    document.querySelectorAll('[data-scr-modal]:not([hidden])').forEach(function (box) {
      close(box.dataset.scrModal);
    });
  });

  /* Every error at once, each as a point -- see DESIGN.md section 3. */
  function showMessages(options) {
    var parts = open('message');
    if (!parts) { window.alert(options.items.map(function (i) { return i.message; }).join('\n')); return; }

    parts.box.querySelector('[data-message-title]').textContent = options.title;
    parts.box.querySelector('[data-message-sub]').textContent = options.subtitle || '';

    var list = parts.box.querySelector('[data-message-list]');
    list.innerHTML = '';
    options.items.forEach(function (item) {
      var li = document.createElement('li');
      li.className = 'scr-msg-item' + (options.neutral ? ' scr-msg-item-neutral' : '');
      if (item.field) {
        var field = document.createElement('span');
        field.className = 'scr-msg-field';
        field.textContent = item.field;
        li.appendChild(field);
        li.appendChild(document.createTextNode(' '));
      }
      li.appendChild(document.createTextNode(item.message || ''));
      if (item.count !== undefined) {
        var count = document.createElement('span');
        count.className = 'scr-msg-count';
        count.textContent = item.count;
        li.appendChild(count);
      }
      list.appendChild(li);
    });

    var old = parts.box.querySelector('.scr-msg-note');
    if (old) old.remove();
    if (options.note) {
      var note = document.createElement('p');
      note.className = 'scr-msg-note';
      note.textContent = options.note;
      list.parentNode.appendChild(note);
    }
  }

  function confirmThen(options, run) {
    var parts = open('confirm');
    if (!parts) { if (window.confirm(options.body)) run(); return; }

    parts.box.querySelector('[data-confirm-title]').textContent = options.title;
    parts.box.querySelector('[data-confirm-sub]').textContent = options.subtitle || '';
    parts.box.querySelector('[data-confirm-body]').textContent = options.body;

    var ok = parts.box.querySelector('[data-confirm-ok]');
    ok.textContent = options.action || 'Delete';
    // Replace the node so a previous record's handler cannot fire on this one.
    var fresh = ok.cloneNode(true);
    ok.parentNode.replaceChild(fresh, ok);
    fresh.addEventListener('click', function () { close('confirm'); run(); });
  }

  function toast(message) {
    if (typeof Swal === 'undefined') return;
    Swal.fire({
      text: message, icon: 'success', toast: true, position: 'top-end',
      showConfirmButton: false, timer: 2200, timerProgressBar: true
    });
  }

  /* Survives the reload that follows a save. */
  function toastAfterReload(message) {
    try { sessionStorage.setItem('byky.toast', message); } catch (err) { /* private mode */ }
  }

  window.addEventListener('DOMContentLoaded', function () {
    var pending;
    try { pending = sessionStorage.getItem('byky.toast'); sessionStorage.removeItem('byky.toast'); }
    catch (err) { pending = null; }
    if (pending) toast(pending);
  });

  /* Shared with screens that save without a drawer (byky-working-time.js),
     so the CSRF handling, the toast and the error modal exist once. */
  window.BykyCrud = {
    post: post, postFile: postFile, toast: toast, toastAfterReload: toastAfterReload,
    showMessages: showMessages, open: open, close: close,
    // byky-fare.js: deleting a season from the fare form asks first, in the same modal.
    confirmThen: confirmThen
  };

  /* ── reading the drawer ───────────────────────────────────────────── */

  function collect(form) {
    var data = {};
    form.querySelectorAll('[data-field]').forEach(function (el) {
      var key = el.dataset.field;
      if (el.classList.contains('byky-multi')) {
        data[key] = Array.prototype.slice
          .call(el.querySelectorAll('input[type="checkbox"]:checked'))
          .map(function (box) { return box.value; });
      } else if (el.type === 'checkbox') {
        data[key] = el.checked;
      } else {
        data[key] = el.value;
      }
    });
    // The footer Active switch lives outside the field sections.
    var active = form.querySelector('.byky-foot-active [data-field]');
    if (active) data.is_active = active.checked;
    return data;
  }

  function reset(drawer) {
    if (drawer.__bykyFill) drawer.__bykyFill('add', null);
  }

  /* ── save ─────────────────────────────────────────────────────────── */

  document.addEventListener('click', function (e) {
    var button = e.target.closest('[data-scr-save]');
    if (!button) return;
    var form = button.closest('form[data-scr-form]');
    var drawer = button.closest('[data-byky-drawer]');
    if (!form || !drawer || !drawer.dataset.saveUrl) return;

    e.preventDefault();
    var again = button.dataset.scrSave === 'new';
    button.disabled = true;

    post(drawer.dataset.saveUrl, collect(form)).then(function (result) {
      button.disabled = false;
      var body = result.body;

      if (body.ok) {
        if (again) {
          reset(drawer);
          toast(body.message);
          return;
        }
        toastAfterReload(body.message);
        window.location.reload();
        return;
      }

      showMessages({
        title: result.status === 403 ? 'Not allowed' : 'Please fix the following',
        subtitle: (body.errors || []).length > 1 ? body.errors.length + ' things need attention' : '',
        items: body.errors || [{ field: '', message: 'Something went wrong.' }]
      });
    });
  });

  /* ── block / unblock ──────────────────────────────────────────────────
     An action, not a form: one button, and the server decides what it means.
     The panel retints itself in byky-hrms.js; this posts it. ─────────────── */

  document.addEventListener('click', function (e) {
    var button = e.target.closest('[data-block-confirm]');
    if (!button || !button.dataset.blockUrl) return;
    e.preventDefault();

    var pick = function (name) { return document.querySelector('[data-block-' + name + ']'); };
    var employee = pick('employee');
    var action = pick('action');
    var blocking = action && /block/i.test(action.value) && !/unblock/i.test(action.value);

    if (!employee || !employee.value) {
      showMessages({
        title: 'Please fix the following',
        items: [{ field: 'Employee', message: 'Choose who this is about.' }]
      });
      return;
    }

    var name = employee.options[employee.selectedIndex].textContent.trim();
    confirmThen({
      title: (blocking ? 'Block ' : 'Unblock ') + name + '?',
      subtitle: 'Employee',
      action: blocking ? 'Block' : 'Unblock',
      body: blocking
        ? 'They will be signed out everywhere and cannot sign in again until they are unblocked.'
        : 'They will be able to sign in again.'
    }, function () {
      button.disabled = true;
      post(button.dataset.blockUrl, {
        employee: employee.value,
        action: blocking ? 'block' : 'unblock',
        reason: (pick('reason') || {}).value || '',
        effective_date: (pick('date') || {}).value || '',
        remarks: (pick('remarks') || {}).value || ''
      }).then(function (result) {
        button.disabled = false;
        if (result.body.ok) {
          toastAfterReload(result.body.message);
          window.location.reload();
          return;
        }
        showMessages({
          title: result.status === 403 ? 'Not allowed' : 'Please fix the following',
          items: result.body.errors || [{ field: '', message: 'Something went wrong.' }]
        });
      });
    });
  });

  /* ── users: reset password, unlock ────────────────────────────────────
     Both are row actions, and both live in the kebab menu, which is detached
     to a body layer while open -- so the row is remembered when the menu opens
     (see the handler above Delete) rather than looked up here. */

  function rowFor(button) {
    var menu = button.closest('.scr-menu');
    return button.closest('.scr-row') || (menu && menu.__bykyRow);
  }

  function rowName(row) {
    return (row.querySelector('.scr-co-name') || row.querySelector('td')).textContent.trim();
  }

  document.addEventListener('click', function (e) {
    var button = e.target.closest('[data-user-reset]');
    if (!button) return;
    var row = rowFor(button);
    var screen = document.querySelector('[data-user-reset-url]');
    if (!row || !row.dataset.pk || !screen) return;

    e.preventDefault();
    var url = screen.dataset.userResetUrl.replace('0', row.dataset.pk);
    var parts = open('password');
    if (!parts) return;

    parts.box.querySelector('[data-password-sub]').textContent = rowName(row);
    var input = parts.box.querySelector('[data-password-input]');
    input.value = '';
    input.focus();

    var ok = parts.box.querySelector('[data-password-ok]');
    /* Replace the node so the previous row's handler cannot fire on this one. */
    var fresh = ok.cloneNode(true);
    ok.parentNode.replaceChild(fresh, ok);
    fresh.addEventListener('click', function () {
      fresh.disabled = true;
      post(url, { password: input.value }).then(function (result) {
        fresh.disabled = false;
        if (result.body.ok) {
          input.value = '';
          close('password');
          toastAfterReload(result.body.message);
          window.location.reload();
          return;
        }
        showMessages({
          title: result.status === 403 ? 'Not allowed' : 'Please fix the following',
          items: result.body.errors || [{ field: '', message: 'Something went wrong.' }]
        });
      });
    });
  });

  document.addEventListener('click', function (e) {
    var button = e.target.closest('[data-user-unlock]');
    if (!button) return;
    var row = rowFor(button);
    var screen = document.querySelector('[data-user-unlock-url]');
    if (!row || !row.dataset.pk || !screen) return;

    e.preventDefault();
    post(screen.dataset.userUnlockUrl.replace('0', row.dataset.pk), {}).then(function (result) {
      if (result.body.ok) {
        toastAfterReload(result.body.message);
        window.location.reload();
        return;
      }
      showMessages({
        title: 'Not allowed',
        items: result.body.errors || [{ field: '', message: 'Something went wrong.' }]
      });
    });
  });

  /* ── the privilege grid ───────────────────────────────────────────────
     byky-screen.js still owns the list <-> matrix swap, Back, and each row's
     Select All -- that is presentation and it was already right. What it could
     not do is persist: in the wireframe Add cloned a <template>, Remove dropped
     a row from the page, and Save did nothing at all.

     Every write here reloads the screen afterwards. That is not laziness: the
     Full Access badge, the Add Role menu and the matrices all derive from the
     same rows, and re-deriving them in JS is how the two drift apart. It also
     drops the wireframe's bug where a removed role could never be added back,
     because its <template> had been consumed. */

  /* Add Role and Remove sit inside a .scr-menu, which is detached to a
     body-level layer while open, so closest() no longer finds the root from
     them. There is exactly one privilege root per page, so falling back to it
     by selector is unambiguous rather than a guess. */
  function privRoot(el) {
    return el.closest('[data-scr-privilege]') || document.querySelector('[data-scr-privilege]');
  }

  function privPost(url, payload, failTitle) {
    post(url, payload).then(function (result) {
      if (result.body.ok) {
        toastAfterReload(result.body.message);
        window.location.reload();
        return;
      }
      showMessages({
        title: result.status === 403 ? 'Not allowed' : failTitle,
        items: result.body.errors || [{ field: '', message: 'Something went wrong.' }]
      });
    });
  }

  document.addEventListener('click', function (e) {
    var button = e.target.closest('[data-priv-add-role]');
    if (!button) return;
    var root = privRoot(button);
    if (!root) return;
    e.preventDefault();
    privPost(root.dataset.privAddUrl, {
      role: button.dataset.privAddRole, module: root.dataset.privModule
    }, 'Could not add that role');
  });

  document.addEventListener('click', function (e) {
    var button = e.target.closest('[data-priv-remove-role]');
    if (!button) return;
    var root = privRoot(button);
    if (!root) return;
    e.preventDefault();

    var name = button.dataset.privRoleName || 'this role';
    confirmThen({
      title: 'Remove ' + name + ' from this module?',
      subtitle: 'Privileges',
      action: 'Remove',
      body: 'It loses every screen in this module. Its other modules are untouched, and it can be added back.'
    }, function () {
      privPost(root.dataset.privRemoveUrl, {
        role: button.dataset.privRemoveRole, module: root.dataset.privModule
      }, 'Could not remove that role');
    });
  });

  document.addEventListener('click', function (e) {
    var button = e.target.closest('[data-priv-save]');
    if (!button) return;
    var root = privRoot(button);
    var card = button.closest('.scr-priv-matrix');
    if (!root || !card) return;
    e.preventDefault();

    /* What is ticked, per screen. Everything else is cleared by the server, so
       unticking is as real as ticking. */
    var grants = {};
    card.querySelectorAll('input[data-page][data-action]').forEach(function (box) {
      if (!box.checked) return;
      (grants[box.dataset.page] = grants[box.dataset.page] || []).push(box.dataset.action);
    });

    button.disabled = true;
    post(root.dataset.privSaveUrl, {
      role: button.dataset.privSave, module: root.dataset.privModule, grants: grants
    }).then(function (result) {
      button.disabled = false;
      if (result.body.ok) {
        toastAfterReload(result.body.message);
        window.location.reload();
        return;
      }
      showMessages({
        title: result.status === 403 ? 'Not allowed' : 'Could not save',
        items: result.body.errors || [{ field: '', message: 'Something went wrong.' }]
      });
    });
  });

  /* ── delete ───────────────────────────────────────────────────────── */

  /* The kebab menu is moved to a body-level layer while it is open
     (byky-screen.js), so by the time Delete is clicked the button is no longer
     inside its row and closest('.scr-row') finds nothing. Remember the row when
     the menu opens, while it is still in place. */
  document.addEventListener('click', function (e) {
    var toggle = e.target.closest('[data-scr-menu-toggle]');
    if (!toggle || !toggle.parentElement) return;
    var menu = toggle.parentElement.querySelector('.scr-menu');
    if (menu) menu.__bykyRow = toggle.closest('.scr-row');
  }, true);

  document.addEventListener('click', function (e) {
    var button = e.target.closest('[data-scr-delete]');
    if (!button) return;
    var menu = button.closest('.scr-menu');
    var row = button.closest('.scr-row') || (menu && menu.__bykyRow);
    var scope = row && row.closest('[data-scr-delete-url]');
    if (!row || !row.dataset.pk || !scope) return;   // a screen that has not opted in

    e.preventDefault();

    var name = (row.querySelector('.scr-co-name') || row.querySelector('td')).textContent.trim();
    var noun = scope.dataset.scrNoun || 'record';
    var url = scope.dataset.scrDeleteUrl.replace('0', row.dataset.pk);

    confirmThen({
      title: 'Delete ' + name + '?',
      subtitle: noun,
      body: 'This removes it permanently. If it is used anywhere it is deactivated instead, and you will be told where.'
    }, function () {
      post(url, {}).then(function (result) {
        var body = result.body;
        if (body.ok) {
          toastAfterReload(body.message);
          window.location.reload();
          return;
        }
        if (body.code === 'in_use') {
          showMessages({
            title: body.title,
            subtitle: body.subtitle,
            neutral: true,
            items: (body.links || []).map(function (link) {
              return { field: link.label, message: '', count: link.count };
            }),
            note: body.note
          });
          return;
        }
        showMessages({
          title: 'Not allowed',
          items: body.errors || [{ field: '', message: 'Something went wrong.' }]
        });
      });
    });
  });
})();

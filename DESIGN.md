# Screen design — our extension

The screens are built on the `.scr-*` system that came with the wireframe, documented
at `~/Desktop/2026/byky-main/byky-screen-design-system.md`. Read that first: it covers
the screen skeleton, the list + drawer pattern, section cards, filtering and the
`.scr-*` naming rules.

This file covers what that system did not need, because the wireframe never saved
anything: **the moments where the server answers back**.

Its §9 rule applies to everything here — extend with the same `scr-` prefix, generalise
from a real need, say in a comment why an addition exists, and re-check the other
screens afterwards, because both shared files are loaded everywhere.

---

## 1. Feedback, by weight

Four levels. Picking the wrong one is the usual mistake: a toast for something that
needs a decision, or a modal for something nobody needs to read.

| Weight | Pattern | Use for |
|---|---|---|
| Light | **Toast** | It worked. "Branch saved." Auto-dismisses |
| Medium | **Message modal** | The person must read and act: validation errors, a delete that was blocked |
| Heavy | **Confirm modal** | A decision that cannot be undone: delete |
| — | **Inline** | Nothing yet. Field-level error text is deliberately *not* used — see §3 |

All three modals reuse `.scr-modal` and its parts, which already exist in
`byky-screen.css`: `.scr-modal-veil`, `.scr-modal`, `.scr-modal-head`, `.scr-modal-title`,
`.scr-modal-sub`, `.scr-modal-body`, `.scr-modal-foot`, closed by `[data-scr-modal-close]`.
Nothing here invents a new look.

**A modal carries its own typography.** `.scr` sets the Sora family, the ink and
the margin resets for everything inside a screen — but a modal is fixed to the
viewport and is included *after* that wrapper closes, so it inherits the page
font instead and reads as a different product. `.scr-modal` therefore names the
family, size, colour and resets itself, including `font-family: inherit` on
buttons, which the CSS reboot otherwise points back at the body font. Any new
overlay included outside `.scr` needs the same.

---

## 2. Confirm modal — `byky/partials/confirm_modal.html`

Replaces the stock SweetAlert2 box the wireframe used, which did not look like the rest
of the screen.

```
┌──────────────────────────────────────┐
│  Delete Operations?              ✕   │   title names the record
│  Department · BYKY                   │   .scr-modal-sub: what it is
├──────────────────────────────────────┤
│  This removes it permanently. If it  │   one line, in plain words
│  is used anywhere, it is deactivated │
│  instead and you will be told where. │
├──────────────────────────────────────┤
│                 Cancel    [ Delete ] │   secondary + .scr-btn-danger
└──────────────────────────────────────┘
```

Rules:

- **The title names the record**, never "Are you sure?". A person confirming should be
  able to see what they are about to lose without reading the body.
- **The body says what will actually happen**, including the deactivate fallback, so the
  outcome is never a surprise.
- The dangerous button carries the danger colour and the verb — `Delete`, not `OK`.
- Cancel is first, and closing the modal is the same as cancelling.

---

## 3. Message modal — `byky/partials/message_modal.html`

One modal, two jobs, because both are "the server has something you must read".

**Validation errors — all of them, at once:**

```
┌──────────────────────────────────────┐
│  Please fix the following        ✕   │
│  3 fields need attention             │
├──────────────────────────────────────┤
│  ● Branch Code  This field is        │   .scr-msg-item, red point
│    required.                         │
│  ● Branch Name  A branch with this   │
│    name already exists.              │
│  ● Hotel Commission  Enter a number. │
├──────────────────────────────────────┤
│                             [ Close ]│
└──────────────────────────────────────┘
```

**Why a list and not field-level text:** the drawer is a scrolling offcanvas with up to
twenty fields across four sections. An error under a field the person cannot see reads
as nothing happening. One list, at the front, shows the whole job at once — and the
field label leads each line, so it is still obvious *where* to fix it. Fixing one error
and discovering the next on the following save is the behaviour this is designed to
avoid.

**A blocked delete — where it is still linked:**

```
┌──────────────────────────────────────┐
│  Operations is still in use      ✕   │
│  Department · BYKY                   │
├──────────────────────────────────────┤
│  ● Branches                       3  │   .scr-msg-count on the right
│  ● Users                          1  │
│                                      │
│  It has been deactivated instead,    │   .scr-msg-note
│  so nothing that uses it breaks.     │
└──────────────────────────────────────┘
```

The counts come from the database's own `ProtectedError` — the real references, not a
guess at which tables might point here.

---

## 4. Toast

The quiet confirmation after a save or a delete. Top-right, auto-dismissing, no button.
The pattern already existed inline in one handler in `byky-screen.js`; it is now a
shared helper in `byky-crud.js` so every screen says it the same way.

Write the sentence in the past tense, name the thing, and stop: **"Branch saved."**,
**"Operations deleted."** No exclamation marks, no "Successfully".

---

## 5. Read-only monitor screens

Some screens only report: attendance punches, alerts, an audit trail. They are
fed by devices or by other modules, and nothing on them is created by hand.

They are **the list screen minus its verbs**, not a new kind of page:

| Keep | Drop |
|---|---|
| `scr-head` with breadcrumb, title, sub | the Add button |
| `scr-tiles` — the figures that matter today | the drawer, entirely |
| `scr-toolbar`: search + `scr-filter-wrap` per dimension | `row_actions` (no Edit, no Delete) |
| `scr-table`, `scr-pager`, `scr-empty` | the confirm modal |

Rules:

- **Export may stay**, gated on `perm.print`. It is the one verb a monitor has.
- **A row is not clickable** unless there is somewhere to go. Do not fake a
  detail view.
- **Two date columns, doing two jobs.** The timestamp renders in the company's
  timezone; the day column shows `business_date`, the day the record counts as.
  They disagree either side of midnight, and that is the point
  (`design/00-findings.md` §7-8).
- **An empty monitor says so, with its headers showing.** "No attendance
  recorded yet." A screen that hides its own structure teaches nobody what it
  will hold — and here it is the honest state until the apps exist.

The first of these is Attendance (`apps/crew/templates/crew/attendance_list.html`);
copy it rather than starting from a list screen and deleting things.

---

## 6. Writing the words

- **Say what happened, not how the system feels about it.** "Branch saved", never
  "Success!".
- **An error says what is wrong and what to do**: "A branch with this code already
  exists. Use a different code." Not "Invalid input".
- **Name the record** in titles: "Delete Corniche Station?" beats "Delete branch?".
- **Never show a field name from the database.** `short_code` is "Branch Code" to the
  person reading it — the label the drawer already uses.

---

## 7. Where the pieces live

| Piece | File |
|---|---|
| Confirm modal markup | `theme/templates/byky/partials/confirm_modal.html` |
| Message modal markup | `theme/templates/byky/partials/message_modal.html` |
| Styles | `static/css/byky-screen.css`, the `.scr-msg-*` block at the end |
| Behaviour: submit, delete, modals, toast | `static/js/byky-crud.js` |

A screen gets all of it by including the two partials and loading `byky-crud.js`.

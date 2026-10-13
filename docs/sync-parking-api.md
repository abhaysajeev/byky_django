# Sync Parking — API Guide

For the operator app developers (the manager app's attendance call works the
same way). It covers how a tablet hands a call the server keeps refusing to
the office, instead of keeping it in its queue for ever. Live in Swagger
(`/api/docs/`): every parkable call lists `parked` and `sync_dismissed`, and
the status call is under **Sync Parking**. Swagger is the final word if this
page and it ever disagree.

> Base URL: `https://<server>/api/v1/<app>/...` — every call is `POST`, JSON
> in and out, in the usual `{credentials, request_data}` envelope.

---

## 1. Which calls can be parked

Calls that record something:

| Call | URL |
|---|---|
| Booking | `orders` |
| Add / replace / remove vehicle | `orders/add`, `orders/replace`, `orders/remove` |
| Return | `orders/return` |
| Payments | `orders/payments` |
| Settle | `orders/settle` |
| Credit note request / cancel | `orders/credit-notes`, `orders/credit-notes/cancel` |
| Manager request / withdraw / reprint used | `orders/requests`, `orders/requests/withdraw`, `orders/requests/used` |
| Card discount approval | `card-discounts/approval` |
| Attendance | `attendance/mark` |

`park` sent on any other call (lookups, lists, login, status calls) is ignored.
Customer registration is **not** parkable: a booking needs the server's
`customer_id`, so register online first.

---

## 2. Parking a call

When a call is refused and the operator cannot fix it on the tablet, send
**exactly the same call again** (same URL, same `sync_id`, same
`request_data`) with one flag:

```json
{ "credentials": { "park": true }, "request_data": { ...as sent before... } }
```

The server runs it once more. If it now goes through, the answer is the
normal `ok`. If it is still refused — or the server crashes on it — it is kept
for the office and answered:

```json
{
  "code": "parked",
  "message": "Sent to the office for correction.",
  "data": {
    "issue_id": 12,
    "sync_id": "01923f8e-…",
    "order_id": "01923e1c-…",
    "waiting_on": null,
    "error": { "code": "amount_mismatch", "message": "…" }
  }
}
```

- **Treat `parked` like success:** remove the call from the queue and keep
  `issue_id` to show on the card.
- Sending the same `sync_id` again — with or without `park`, even with
  corrected data — returns the same `parked`. Nothing re-runs; from now on the
  office fixes it. A lost reply is therefore harmless.
- `order_id` is the order the call belongs to; `null` for attendance.

### What can and cannot be parked

| Reply | What to do |
|---|---|
| `ok`, `duplicate`, `parked` | Done. Remove from the queue. |
| `sync_dismissed` (409) | The office dismissed it (see 4). Remove from the queue. |
| No reply, timeout, 502 / 503 / 504, `rate_limited` | The server never took it. Keep it and retry later; it **cannot** be parked. |
| `not_authenticated` (401) | Sign in again, then resend. Never parked. |
| Any other refusal on a parkable call | Show it on the card in red. The operator fixes it (resend with the **same** `sync_id`) or parks it. |

---

## 3. Calls waiting behind a parked one

Once an order has a parked call, **every later call for that order is parked
behind it automatically** — from any tablet, with or without `park`. The
reply is `parked` with `waiting_on` set to the issue it waits behind and
`error: null`:

```json
{ "code": "parked", "data": { "issue_id": 13, "order_id": "01923e1c-…", "waiting_on": 12, "error": null } }
```

Treat the order as handed to the office. When the office replays issue 12,
issue 13 runs right after it, in the order the calls arrived.

A withdraw or reprint-used follows the request it names, and a credit note
cancel follows its credit note — even when that request or note is itself
the parked call. Attendance never waits behind anything.

---

## 4. Asking what happened — `sync/issues/status`

Read-only. Use it before logout, or to clear old cards.

```json
{ "credentials": {}, "request_data": { "issue_ids": [12, 13] } }
```

`issue_ids` is optional (up to 100). Without it you get every issue of **this
device** the server still keeps: open ones always, resolved and dismissed ones
for 45 days; newest first, at most 500.

```json
{
  "code": "ok",
  "data": { "issues": [
    { "issue_id": 12, "sync_id": "…", "kind": "return", "order_id": "…",
      "status": "resolved", "waiting_on": null, "dismissed_with": null,
      "dismiss_reason": "", "error": null, "resolved_at": "2026-10-14T09:12:00+00:00" },
    { "issue_id": 13, "sync_id": "…", "kind": "settle", "order_id": "…",
      "status": "dismissed", "waiting_on": null, "dismissed_with": 12,
      "dismiss_reason": "Settled on paper", "error": null, "resolved_at": null }
  ] }
}
```

| `status` | Meaning |
|---|---|
| `open` | Still with the office. `waiting_on` set if it waits behind another issue. |
| `resolved` | The office replayed it and it was recorded. Drop the card. |
| `dismissed` | The office gave up on it; `dismiss_reason` says why. `dismissed_with` is set when it was dismissed together with the issue it waited behind. |

---

## 5. Export & log out

Before logging out, send everything still unresolved (red cards, and calls
stuck on "try again later") with `park: true`, **in the order the calls were
created**, so a call is parked before the calls that follow it.

---

## 6. Receipt numbers

The tablet keeps raising its own receipt counter as now. The server also
raises its copy from a parked booking's `order_no`, so a reinstalled tablet
never gets that number again. No extra call is needed.

---

## 7. What the office does (for reference)

On the web, **Logs › Sync Issues** (system administrators): open the issue,
correct the request JSON, and press **Replay** — it runs as the original
operator, device and station, even if that session has ended or the tablet has
moved. Calls waiting behind it run next. Or **Dismiss** it with a reason; the
calls waiting behind it are dismissed with it.

# Manager App — API Guide

For the manager app developers. It covers what the manager app calls, in the
order it calls them: sign in → load the masters (states, stations) → requests
(list, approve, reject, revoke) → reports. Everything here is live in Swagger
(`/api/docs/`) under the tags **Manager Auth**, **Manager Masters**,
**Manager Requests** and **Manager Reports** — Swagger is generated from the
code and is the final word if this page and it ever disagree.

> Base URL: `https://<server>/api/v1/manager/...` — every call is `POST`,
> JSON in and out.

---

## 1. Rules every call follows

### 1.1 The envelope

Every request body:

```json
{ "credentials": {}, "request_data": { ... } }
```

- `credentials` — send `{}`. (Swagger shows `additionalProp1…` placeholders;
  ignore them.)
- `request_data` — the call's fields. For a call with no fields, send `{}`.

Every reply:

```json
{ "code": "ok", "message": "Human text", "data": { ... } }
```

- **Switch on `code`, never on `message`.** `message` is for display/logs and
  may be reworded.
- `code` is `"ok"` on success. Anything else is a refusal; its HTTP status is
  set accordingly (400, 401, 403, 404, 409, 429, 500).

### 1.2 The access token

All calls after login send the access token:

```
Authorization: Bearer <access token>
```

| Situation | Reply | What the app does |
|---|---|---|
| No/expired/invalid token | `401 not_authenticated` | Call `auth/refresh`; if that fails with `session_closed`, show login |
| Token from another app (operator, employee) | `403 wrong_channel` | Bug in the app — the manager app must only use its own token |

A manager token is the **only permission** these calls need: any signed-in
manager may list, approve, reject and revoke requests and read every report of
their company. There are no per-screen rights in the manager app.

### 1.3 Bad input — `invalid_request`

A missing or malformed field answers **400**:

```json
{ "code": "invalid_request", "message": "to_date must not be before from_date.",
  "data": { "errors": { "to_date": "must not be before from_date" } } }
```

`data.errors` is `{field: "what is wrong"}` — show it next to that field.

### 1.4 Formats

| Thing | Format | Example |
|---|---|---|
| Date (input and output) | `YYYY-MM-DD` | `"2026-10-09"` |
| Date-time (output) | `YYYY-MM-DD HH:MM:SS`, **company time**, no offset | `"2026-10-09 16:40:00"` |
| Money | **string**, 3 decimals | `"110.125"` |
| Percentage | **string**, 2 decimals | `"10.00"` |
| Ids | `state_id`, `branch_id` — whole numbers; `request_sync_id`, `order_id` — UUID strings | |

Never parse money into a float — keep the string, or use a decimal type.
Dates you send are **company-local days**, and `from_date`/`to_date` include
**both** days.

### 1.5 Paging (every list)

Lists come back **50 rows at a time**, newest first.

- Send `page` (from **1**; leave it out for page 1).
- The reply carries:

| Field | Meaning |
|---|---|
| `page` | the page you got |
| `pages` | how many pages exist for these filters |
| `total` (`total_orders` in `reports/orders`) | how many rows match the filters overall |

How to use it:

- **Infinite scroll:** load page 1; at the bottom, if `page < pages`, load
  `page + 1` and append. Stop when `page == pages`.
- **Filters first, then paging.** Every filter narrows the list **on the
  server**, and pages are cut from the filtered result — `total`/`pages` always
  describe the filtered list. Do not filter on the device: you only hold one
  page, so you would miss rows on other pages.
- **When any filter changes, go back to page 1.**
- A page past the end (e.g. `page: 9` when `pages` is 3) returns the **last
  page**, not an empty list — stop on `page < pages`, not on an empty reply.
- New rows arriving while the user scrolls push the list down, so the next
  page may repeat a row. **De-duplicate by id** (`request_sync_id`, `order_id`)
  when appending, or reload from page 1 on pull-to-refresh.

---

## 2. The lifecycle at a glance

```
1. device/registration ──► approved
2. auth/login ───────────► tokens + employee block        (store tokens)
3. states ───────────────► state_id list                  (store; picker)
4. branches ─────────────► branch_id list (+ state_id)    (store; picker)
5. requests/list ────────► filter by state_id / branch_id / kind / pending / dates
     └► requests/approve | requests/reject | requests/revoke
6. reports/collections/states   (from_date, to_date)
     └► reports/collections/branches   (+ state_id)
          └► reports/orders              (+ branch_id)
7. auth/refresh when the access token expires; auth/logout on sign-out
```

---

## 3. Before login

### 3.1 Register the phone — `POST /api/v1/manager/device/registration`

Every app registers its device once; an admin approves it on the web.

```json
{ "credentials": {}, "request_data": {
    "installation_id": "74a1cebc-2aeb-4670-96f0-3550fc72db63",
    "company_id": "0598",
    "platform": "android",
    "platform_id": "615d433171487027",
    "device_model": "Xiaomi Redmi Note 9 Pro Max"
} }
```

| Field | Required | Notes |
|---|---|---|
| `installation_id` | **yes** | A UUID the app generates **once** and stores. A reinstall makes a new one — that is fine |
| `platform` | **yes** | `android` or `ios` |
| `platform_id` | **yes** | Android ID / iOS identifierForVendor — how the server recognises the same phone after a reinstall |
| `company_id` | always send it | The company code (e.g. `"0598"`). Without it a server hosting several companies answers `registration_unavailable` |
| `device_model` | no | Shown to the admin |

Replies: `approved` (200) → go to login · `pending_approval` (202) → show
"waiting for approval" and poll · `reconnect_pending` (202, after a reinstall,
same `device_registration_id`) → poll · `device_blocked` / `device_retired` /
`reinstall_rejected` (403) → show the message.

The **`device_registration_id` never changes** for this phone — show it on the
waiting screen so the admin can find it.

### 3.2 Sign in — `POST /api/v1/manager/auth/login`

```json
{ "credentials": {}, "request_data": {
    "username": "MGR001", "password": "••••••••",
    "installation_id": "74a1cebc-2aeb-4670-96f0-3550fc72db63"
} }
```

Reply `ok`:

```json
{ "code": "ok", "message": "Welcome, Meera K.", "data": {
    "tokens": {
      "access": "eyJ…", "refresh": "eyJ…",
      "access_expires_at": "2026-10-14T05:46:16+00:00",
      "refresh_expires_at": "2026-11-08T05:16:16+00:00"
    },
    "employee": {
      "employee_code": "MGR001", "full_name": "Meera K", "first_name": "Meera",
      "middle_name": "", "last_name": "K", "designation": "Manager", "role": null,
      "branch_id": null, "branch": null, "mobile": "0500000000",
      "email": "meera@byky.test", "photo_url": null, "company": "BY KY SPORT & ..."
    }
} }
```

- **Store** both tokens and their expiry times securely.
- `employee.branch_id` is the manager's home station, often `null` — a manager
  works across **every station of the company**.
- Refusals worth handling: `invalid_credentials` (401), `account_locked`
  (429), `wrong_channel` (this user may not use the manager app),
  `user_blocked`, `device_pending_approval` (202), `device_blocked`,
  `device_retired`, `device_not_registered` (409 — register first).

### 3.3 Keep the session — `auth/refresh` and `auth/logout`

- **`POST /api/v1/manager/auth/refresh`** `{ "refresh_token": "<refresh>" }` →
  a **new pair** of tokens. Both rotate: the old refresh token stops working
  immediately, so save the new pair at once. Only `session_closed` (401) means
  "show login"; a network error or 5xx must **not** log the user out — retry.
- **`POST /api/v1/manager/auth/logout`** — with the `Authorization` header, empty
  `request_data`. Closes this session.

---

## 4. Masters — the ids every other call uses

Load both right after login and keep them for the session (refresh on
pull-to-refresh or once a day). They feed the state and station pickers, and
their ids are what `requests/list` and the reports take.

### 4.1 States — `POST /api/v1/manager/states`

Request: `{ "credentials": {}, "request_data": {} }` — no fields.

```json
{ "code": "ok", "message": "States.", "data": { "states": [
    { "state_id": 1, "code": "AUH", "name": "Abu Dhabi", "branch_count": 5 },
    { "state_id": 2, "code": "DXB", "name": "Dubai", "branch_count": 6 }
] } }
```

Only states where the company has **active** stations, sorted by name.

### 4.2 Stations — `POST /api/v1/manager/branches`

| Field | Required | Notes |
|---|---|---|
| `state_id` | no | Only that state's stations. Leave out for all |

```json
{ "code": "ok", "message": "Branches.", "data": { "branches": [
    { "branch_id": 3, "code": "ADC1", "name": "Abu Dhabi Corniche 1",
      "state_id": 1, "state": "Abu Dhabi", "location": "Corniche" }
] } }
```

Active stations only, sorted by name. Each carries its `state_id`, so one
call without `state_id` can build both pickers (group by `state_id`).

**Store:** `state_id → name`, `branch_id → name, state_id`. Use the ids, never
the names, in later calls.

---

## 5. Requests

Operators ask from the tablet; the manager decides here or the back office on
the web — whoever acts first wins, the other gets `request_decided`.

### 5.1 The three kinds

| `kind` | Order | What the manager approves |
|---|---|---|
| `discount` | running | A **rule**: `percent` + value (e.g. `10.00`) or `amount` + AED value (e.g. `15.000`). The bill is not made yet; the tablet applies it at settle and the server checks it |
| `complimentary` | running | The whole bill free (net 0). No value |
| `reprint` | settled | One more print of the bill. No value. Never revoked |
| `cancel` | running | Cancel the whole order (an operator's mistake). Reason always given. No value. Approving cancels the order at once and frees its vehicles. Never revoked |

### 5.2 Statuses

| `status` | Meaning | Manager can |
|---|---|---|
| `pending` | waiting for a decision | approve, reject |
| `approved` | approved | revoke (discount, complimentary — while the order runs) |
| `rejected` | refused (see `decision_note`) | — |
| `revoked` | approved, then taken back (see `revoke_note`) | — |
| `withdrawn` | the operator took it back | — |
| `closed` | still pending when the order settled | — |
| `used` | a reprint, printed (`used_at`) | — |

### 5.3 List — `POST /api/v1/manager/requests/list`

All fields optional — send `{}` for every request, newest first.

| Field | Type | Notes |
|---|---|---|
| `pending` | boolean | `true` = only `pending`. `false` / left out = every status |
| `kind` | string | `discount`, `complimentary`, `reprint` or `cancel` |
| `state_id` | number | Only that state's stations (from `states`) |
| `branch_id` | number | One station (from `branches`). With `state_id`, the station must be in that state, else the list is empty |
| `from_date` | `YYYY-MM-DD` | Asked on or after this day (company time) |
| `to_date` | `YYYY-MM-DD` | Asked on or before this day. Must not be before `from_date` |
| `page` | number | From 1 |

```json
{ "credentials": {}, "request_data": {
    "pending": true, "kind": "discount", "state_id": 1, "branch_id": 3,
    "from_date": "2026-10-01", "to_date": "2026-10-09", "page": 1
} }
```

Reply:

```json
{ "code": "ok", "message": "Requests.", "data": {
  "requests": [ {
    "request_sync_id": "01923e2b-77aa-7b22-8c33-d4e5f6a7b8c9",
    "order_id": "01923e1c-0a11-7b22-8c33-d4e5f6a7b8c9",
    "order_no": "DUBPP60182000231",
    "kind": "discount",
    "status": "pending",
    "reason": "Regular customer",
    "requested_at": "2026-10-08 16:40:00",
    "discount_type": null, "discount_value": null,
    "decided_by": null, "decided_channel": null, "decided_at": null, "decision_note": "",
    "revoked_by": null, "revoked_channel": null, "revoked_at": null, "revoke_note": "",
    "applied_amount": null, "applied_at": null, "used_at": null,
    "branch_id": 3, "station": "Abu Dhabi Corniche 1", "requested_by": "Rashed K",
    "order": {
      "order_no": "DUBPP60182000231", "status": "active",
      "customer_name": "Ahmed Al Mansoori", "customer_mobile": "971501234567",
      "booked_at": "2026-10-08 16:00:00", "advance_paid": "100.000", "net_amount": null,
      "vehicles": [
        { "vehicle": "MO 41", "identifier": "VB1241", "vehicle_type": "Monaco", "status": "active",
          "start_time": "2026-10-08 16:00:00", "expected_end_time": "2026-10-08 17:00:00",
          "end_time": null, "minutes_run": 40 }
      ]
    }
  } ],
  "page": 1, "pages": 1, "total": 1
} }
```

Field notes:

- `request_sync_id` — the request's id; send it to approve / reject / revoke.
- `discount_type` / `discount_value` — set once a discount is approved.
- `decided_channel` / `revoked_channel` — `manager` (this app) or `web`.
- `applied_amount` / `applied_at` — what the settle actually applied.
- `order.vehicles[].minutes_run` — minutes so far for a vehicle still out;
  `order.net_amount` — the settled bill (null while running).

Suggested screens: a **Pending** tab (`pending: true`) and a **History** tab
(no `pending`), each with the state / station / kind / date filters.

### 5.4 Approve — `POST /api/v1/manager/requests/approve`

| Field | Required | Notes |
|---|---|---|
| `request_sync_id` | **yes** | From the list |
| `discount_type` | **discount only** | `percent` or `amount` |
| `discount_value` | **discount only** | `percent`: above 0, max 100, **2 decimals**. `amount`: AED above 0, **3 decimals**. Send as a string |
| `note` | no | Up to 500 characters; the operator sees it |

```json
{ "credentials": {}, "request_data": {
    "request_sync_id": "01923e2b-77aa-7b22-8c33-d4e5f6a7b8c9",
    "discount_type": "percent", "discount_value": "10.00", "note": "Regular"
} }
```

For a **complimentary**, **reprint** or **cancel**, send only `request_sync_id` (and
`note`). Reply: the updated row, same shape as the list.

### 5.5 Reject — `POST /api/v1/manager/requests/reject`

`{ "request_sync_id": "…", "note": "Not eligible" }` — `note` optional.
Only a `pending` request.

### 5.6 Revoke — `POST /api/v1/manager/requests/revoke`

`{ "request_sync_id": "…", "note": "Approved by mistake" }` — `note` optional.
Only an **approved discount or complimentary** while its order is still
running. The operator may then ask again.

### 5.7 Refusals for approve / reject / revoke

| `code` | HTTP | Meaning | App action |
|---|---|---|---|
| `unknown_request` | 404 | No such request in your company | Reload the list |
| `request_decided` | 409 | Already decided (maybe on the web) — message says how | Reload the row |
| `order_closed` | 409 | The order was settled/cancelled meanwhile | Reload the row |
| `request_not_approved` | 409 | Revoke on a request that is not approved | — |
| `request_not_revocable` | 409 | Revoke on a reprint or a cancel | Hide Revoke for reprints and cancels |
| `invalid_request` | 400 | e.g. percent over 100, 3 decimals on a percent, missing type for a discount | Show `data.errors` / `message` |

Always refresh the row (or the list) after any of these — someone else may
have acted first.

### 5.8 Getting new requests

There is no push notification yet. Poll `requests/list` with
`"pending": true` while the Pending screen is open (e.g. every 30–60 s and on
pull-to-refresh), and stop when the app is in the background.

---

## 6. Reports

### 6.1 Collections per state — `POST /api/v1/manager/reports/collections/states`

| Field | Required | Notes |
|---|---|---|
| `from_date` | **yes** | `YYYY-MM-DD` |
| `to_date` | **yes** | `YYYY-MM-DD`, not before `from_date`. No limit on the range |

```json
{ "code": "ok", "message": "Collections.", "data": {
  "from_date": "2026-10-01", "to_date": "2026-10-08",
  "states": [
    { "state_id": 2, "code": "DXB", "name": "Dubai", "orders": 412,
      "net_amount": "18450.500", "credit_notes": "70.000" },
    { "state_id": 9, "code": "FUJ", "name": "Fujairah", "orders": 0,
      "net_amount": "0.000", "credit_notes": "0.000" }
  ],
  "total": { "orders": 1033, "net_amount": "45210.250", "credit_notes": "120.000" }
} }
```

- **Collections = settled orders**: each counts once, with its invoice's
  `net_amount`, on the day it was **settled**. Running and cancelled orders are
  not collections.
- `credit_notes` — approved credit notes on those orders, shown **beside** the
  net, **not** subtracted.
- Every state with stations is listed (0 when nothing sold); highest net first.
- Not paged (one row per state).

### 6.2 Collections per station — `POST /api/v1/manager/reports/collections/branches`

| Field | Required | Notes |
|---|---|---|
| `from_date`, `to_date` | **yes** | As above |
| `state_id` | **yes** | From `states` (or the row tapped in 6.1) |

Reply: `from_date`, `to_date`, `state_id`, `branches[]` (`branch_id`, `code`,
`name`, `orders`, `net_amount`, `credit_notes`) and the state's `total`.
Active stations always listed (0 when nothing sold); a closed station only if
it sold in the range. Not paged.

### 6.3 One station's orders — `POST /api/v1/manager/reports/orders`

| Field | Required | Notes |
|---|---|---|
| `branch_id` | **yes** | From `branches` (or the row tapped in 6.2) |
| `from_date`, `to_date` | **yes** | Orders **booked** in this range |
| `order_status` | no | `all` (default), `running`, `partially_received`, `awaiting_settlement`, `fully_received`, `cancelled` |
| `page` | no | From 1 |

```json
{ "code": "ok", "message": "Orders.", "data": {
  "branch_id": 7, "branch_name": "Creek Park Gate 1",
  "from_date": "2026-10-01", "to_date": "2026-10-08", "order_status": "all",
  "summary": { "all": 15, "running": 4, "partially_received": 2, "awaiting_settlement": 1,
               "fully_received": 8, "cancelled": 0, "replaced": 1 },
  "orders": [ {
    "order_id": "01923e1c-…", "order_no": "ABCO1105700000006",
    "station_name": "Creek Park Gate 1",
    "customer_name": "Ahmed Al Mansoori", "customer_mobile": "971501234567",
    "booked_at": "2026-10-08 08:58:10", "start_time": "2026-10-08 09:00:00",
    "end_time": "2026-10-08 11:00:00", "duration_minutes": 120,
    "order_status": "partially_received",
    "amount": "250.000", "advance_paid": "100.000", "invoice_no": null,
    "credit_note": null,
    "vehicles": [
      { "vehicle_name": "QUAD-04", "vehicle_type": "Quad", "vehicle_status": "running",
        "start_time": "2026-10-08 09:00:00", "end_time": null, "minutes": 95 }
    ],
    "payments": [
      { "mode": "Cash", "kind": "advance", "amount": "100.000", "reference_no": "",
        "paid_at": "2026-10-08 08:58:10" }
    ]
  } ],
  "page": 1, "pages": 1, "total_orders": 15
} }
```

- `summary` counts the **whole range** (not just this page) — use it for the
  tab badges. `replaced` overlaps the others (orders with a replaced vehicle).
- `order_status`: `running` — no vehicle back yet; `partially_received` —
  some back; `awaiting_settlement` — all back, not settled; `fully_received` —
  settled; `cancelled`.
- `end_time` — last vehicle back once settled, else the latest expected end.
- `amount` — the settled net, or what the lines come to so far.
- `vehicles[].vehicle_status` — `running`, `returned`, `replaced`, `removed`,
  `cancelled`; `minutes` — run so far.
- `payments[].kind` — `advance`, `settlement` (paid at settle), `refund`
  (handed back).
- Paged (§1.5); `unknown_branch` (404) for a station not in your company.

**Collections vs order details:** collections count settled orders by
**settle day**; order details list orders by **booking day**, running ones
included. They answer different questions and will not always match.

---

## 7. Checklist for the app

- [ ] One stored `installation_id` per install; always send `platform_id`.
- [ ] Store and rotate both tokens; refresh on `not_authenticated`; log out only
      on `session_closed`.
- [ ] Load `states` + `branches` after login; use their ids, not names.
- [ ] Every list: send filters, page from 1, append while `page < pages`,
      de-duplicate by id, reset to page 1 when a filter changes.
- [ ] Money as strings / decimals, never floats.
- [ ] Discount approve: `percent` 2 decimals ≤ 100, `amount` 3 decimals;
      complimentary / reprint: no value.
- [ ] Hide Revoke for reprints, cancels, and anything not `approved`.
- [ ] After any refusal on approve / reject / revoke, reload the row.
- [ ] Switch on `code`, show `message`, show `data.errors` per field.

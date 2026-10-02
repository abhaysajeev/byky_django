# Order lifecycle — design

Status: **P3 built** (2 Oct 2026). Decisions agreed in section 5. Built:
`Order`, `OrderItem`, `Payment`, `OrderEvent`, `Invoice`, `InvoiceItem`
(section 4); `run_once`, `record_payment`, `lock_order`; booking
(`POST /orders`), `POST /orders/detail`, `/orders/add`, `/orders/replace`,
`/orders/remove`, `POST /orders/return`, `POST /orders/settle` (card discount
redeemed, invoice issued), running status on `/vehicles`,
`POST /device/settings`. Next: P4 standalone payments.

The legacy system is the reference, not the template: the business flow is
kept, its bugs are not. Legacy sources are cited as proc names
(`analysis/_procs/<name>.sql`) and QA row counts.

---

## 1. Business flow

1. **Book.** The operator finds or registers the customer (a blocked customer
   is refused), picks one or more vehicles at the station and a package for
   each (fare or offer), and usually takes money up front (the **advance**).
   A discount card is raised as a card-discount request and approved by a
   manager (already built). The tablet prints a receipt with its own receipt
   number. Order **Active**; vehicles **on rent**.
2. **During the rental** (optional):
   - **Replace** — a vehicle breaks and is swapped. The old line closes as
     *Replaced* (with the reason, never billed); a new line starts, linked to
     it and carrying the package price. Only the current line is billed.
   - **Add** — another vehicle joins the order; an advance for it can be taken
     in the same call.
   - **Remove** — a vehicle comes off without a replacement; its line stays on
     the order as *Removed* (with the reason, never billed). A vehicle the
     customer rode is returned, not removed.

   Money sent with a call (`payments`) is recorded in that call by the server
   — never a second request. Each payment is sent once.
3. **Return** — each vehicle comes back on its own: its line becomes
   *Returned* with the real return time and its final amount (overtime
   included). The vehicle is free to rent at once.
4. **Settle** — once every vehicle is back, the bill closes:
   lines total − discounts + tax ± rounding = net. An approved card discount is
   applied and marked *Redeemed*; one still pending is *Cancelled*. Compared
   with what was paid: collect the **balance**, or record a **refund**. Order
   **Completed**; final receipt prints.
   **Direct rental** — the same three calls: book (an advance is optional),
   return (no overtime, so total = base fare), settle (payment may be taken
   here). No special endpoint.
5. **Cancel** — whole order only, before settlement, through manager approval.
   On approval: order and lines *Cancelled*, vehicles freed, money taken is
   recorded as a refund, a pending card discount is cancelled.

### Statuses

| Record | Flow |
|---|---|
| Order | Active → Completed · Active → Cancelled |
| Line | Active → Returned (billed) / Replaced / Removed / Cancelled (the order was cancelled) |

### Money

**Payment entries**, in the manner of ERPNext's Payment Entry: every movement
of money is one record, and an order can have any number of them — part cash,
part card.

| Field | Meaning |
|---|---|
| `sync_id` | the entry's id, made on the tablet; makes a resend safe |
| `order` | the one order it belongs to (no splitting a payment across orders) |
| `kind` | **Advance** (booking or mid-rental), **Settlement** (when the bill is settled) — money in; **Refund** — money out |
| `payment_mode` | Cash, Card, … — per entry |
| `amount` | always positive; `kind` gives the direction |
| `reference_no` / `reference_date` | card slip, cheque |
| `paid_at` | the **tablet's** time |
| tablet, operator | who took it |

- **The order carries no payment mode.** Its `paid_amount` is money in − refunds,
  computed from its entries — never a second copy to disagree with them
  (legacy's `DMSOrder.PaidAmount` vs `SUM(DMSPayment.Amount)` bug).
- **Entries are never edited or deleted.** A mistake is corrected by a
  reversing Refund entry, so the trail stays complete.
- Booking and settlement take a **list** of entries; `POST /orders/payments`
  adds one mid-rental (e.g. an extra advance when the customer extends).
- The invoice copies the payment breakdown by mode at settlement.

**No bill before the ride ends.** Amounts are filled when they are known,
never guessed; until then they are blank (`null`):

| Amount | Filled when |
|---|---|
| line `package_minutes`, `base_fare` (the package price agreed) | booking |
| line `overtime_amount`, `total_amount` (= base fare + overtime) | that vehicle is returned |
| order `subtotal`, `discount_*`, `tax_percentage`, `tax_amount`, `rounding_adjustment`, `net_amount`; `balance_due` | settle |
| order `amount_received`, `amount_refunded`, `paid_amount` | each payment entry, from booking on |

The bill: `net_amount` = `subtotal` − `discount_amount` + `tax_amount` ±
`rounding_adjustment`. No discount and no VAT on a line: the one discount (the
card discount) and VAT (one rate per bill) are on the order.

**Payment status** follows the order: `pending` while active, `paid` once
settled, blank once cancelled. Settle refuses unless the bill is paid in full
(`balance_not_settled`), so `paid` is always true.

### Invoice

An invoice exists only for a **settled** order: settlement creates it in the
same transaction, after full payment, issued by the settling tablet and
operator. A cancelled order never gets one.

- **Number = the order number** (as BYKY works today). The tablet sends
  nothing extra at settlement. A cancelled order's number is simply never
  invoiced; the gap is explained by that cancelled order and its approved
  cancel request. So `order_no` becomes **required and unique per company**.
- **A frozen copy**, never edited after issue: `invoice` (number, issue time,
  company TRN, customer name and phone as at issue, station, tablet,
  operator, subtotal / discount / VAT / rounding / net) and `invoice_item` (one
  row per billed vehicle: package, start / end, amount, discount, tax).
  Replaced lines are not billed and not copied.
- One invoice per order. A later correction is a **credit note** referencing
  the invoice — never an edit (legacy's credit note overwrote `DMSOrder` in
  place). Credit notes are not in this build; the tables leave room for them.

Legacy had no invoice table: `RMS_PRINT_INVOICE` rebuilt it from the order at
print time, so editing the order changed an invoice already issued.

---

## 2. Ids and idempotency

Two kinds of id, never mixed:

- **Target id** — which record a call acts on (order, line). The same on every
  call that touches that record.
- **Request id** — the call's own `sync_id`: which *action* this is. New for each
  action the operator takes, identical on every resend of that action.

| Id | Made by | Purpose |
|---|---|---|
| order `sync_id` | tablet, at booking | the order's permanent id (target) |
| line `sync_id` | tablet, at booking / add / replace | the line's permanent id (target) |
| call `sync_id` | tablet, every action | idempotency + history |

At booking the order's `sync_id` doubles as the call's id — a booking happens
once per order.

Why the tablet makes the ids: it works offline. It must be able to return or
replace a line it booked before the server has ever replied. Order + vehicle is
not a usable key: the same vehicle can appear twice in one order.

### Order history (event) table

Every call is stored as one row: call `sync_id`, order, line(s), action, tablet
time, server time, device, operator, a fingerprint (hash) of the request body,
and the reply that was sent.

### Reliability rules

1. **Resend → `duplicate`, same reply.** A call whose `sync_id` is already stored,
   with the same body, gets the stored reply back with code `duplicate`.
   Nothing is written.
2. **Same id, different body → `sync_id_conflict`** (409). Catches app bugs
   instead of silently accepting one version.
3. **One order's calls in order.** The tablet sends book first, then the rest
   in sequence. A call for an order the server has not got yet is answered
   `order_not_synced` (409, retryable).
4. **Two kinds of error**, each code documented as one or the other:
   - **retry** — network failure, `order_not_synced`, server error: keep the
     call queued and try again;
   - **final** — e.g. `vehicle_already_rented`, `line_not_active`,
     `customer_blocked`, `order_closed`: stop retrying, show the operator.

Every refusal carries `data.retry` (`true` / `false`) so the app need not keep
its own list. The codes, one place in the code (`apps/rental/services.py`
`ORDER_ERRORS`, which Swagger is built from):

| Code | HTTP | Kind | When |
|---|---|---|---|
| `order_not_synced` | 409 | retry | a call for an order the server has not got yet |
| `sync_id_conflict` | 409 | final | the call's id is already used with another body |
| `order_no_used` | 409 | final | the receipt number is already used in the company |
| `vehicle_already_rented` | 409 | final | a vehicle is on another active line |
| `vehicle_repeated` | 400 | final | the same vehicle twice in one booking |
| `item_id_used` / `payment_id_used` | 409 | final | a line or payment id already exists |
| `unknown_customer` / `_payment_mode` / `_vehicle` / `_fare` / `_offer` | 400 | final | not this company's |
| `vehicle_not_at_station` | 400 | final | the vehicle is at another station |
| `unknown_order` | 404 | final | no such order at this station (`/orders/detail`) |
| `order_closed` | 409 | final | the order is completed or cancelled |
| `unknown_item` / `item_not_active` | 404 / 409 | final | the line is not on the order / already returned, replaced or removed |
| `invalid_return_time` | 400 | final | returned before the line's start |
| `invalid_time` | 400 | final | replaced / removed before the line's start |
| `items_still_out` | 409 | final | settle with a vehicle not returned |
| `amount_mismatch` | 400 | final | a figure does not agree (line total, subtotal, net) |
| `balance_not_settled` | 409 | final | after settle's payments, paid ≠ net |
| `unknown_card_claim` / `card_claim_not_approved` / `unknown_card_discount` | 404 / 409 / 404 | final | the discount named at settle cannot be applied |

A constraint lost in a race (two tablets, one vehicle, the same instant) is
reported as the code it means, read from the database constraint's name.

Rules for the app team: every call has a new `sync_id` and a resend reuses it;
orders and lines get their `sync_id` on the tablet; send an order's calls in
order; `ok`/`duplicate` = done, retry code = keep queued, final code = stop and
show.

Later, if tablets spend long periods offline: a batch endpoint taking the whole
queue in one request, with the same `sync_id`s and the same rules.

### What the tablet reads

- **`POST /orders/detail`** — one order, by `sync_id` or `order_no`, in the same
  shape as every order reply. Any tablet at the order's station.
- **`/vehicles`** — each vehicle carries `on_rent`, `rental` (`order_no`,
  `expected_end_time`) and `can_rent` (= `is_available` and not on rent),
  derived from the active lines (decision 6).
- **`POST /device/settings`** — the login's settings block on its own:
  station settings, `order_no_prefix`, `last_order_no` (the last receipt the
  server has seen from this tablet here), `next_order_number`,
  `next_test_number`. Login keeps returning the same block.
- **Receipt counter.** A receipt number is prefix + tablet registration id +
  six or more digits (`DUBPP` + `60182` + `000231`). Each booking raises the
  tablet's `BillContinuity.last_number` to the number inside its `order_no`
  (never lowers it; a number not in this tablet's shape is stored and counts
  nothing) — **a refused booking too**, since its receipt was already
  printed. Every order reply carries `next_order_number`.
- **One prefix.** The tablet builds receipt numbers from the top-level
  `order_no_prefix` (its counter's own, kept if the station's is changed).
  `device_settings` carries no prefix of its own.
- **App rules for offline queues:** at login continue from the *higher* of
  the tablet's own number and the server's (queued bookings have not raised
  the server's yet — taking the server's blindly reissues them, and their
  uploads are refused `order_no_used`); upload the whole queue before logging
  out (a tablet moved to another station would upload its old station's
  bookings under the new one).

---

## 3. Worked example

House style: `POST` with `{credentials, request_data}`, reply
`{code, message, data}`. Ids are shortened here (`O-1`, `L-1`, …); real ones
are UUIDv7. Times are `YYYY-MM-DD HH:MM:SS` in company time.

**Scenario:** customer Ahmed at Corniche 1, tablet TAB-07 (receipt prefix
`DUBPP`, registration id `60182`), two bikes for one hour. One breaks and is swapped; both return, one
late; the bill settles with his approved discount card.

### 3.1 Book — two bikes, AED 100 advance in cash

`POST /api/v1/operator/orders`

```json
{
  "credentials": { "...": "..." },
  "request_data": {
    "sync_id": "O-1",
    "order_no": "DUBPP60182000231",
    "customer_id": 5512,
    "booked_at": "2026-10-02 16:00:05", "start_time": "2026-10-02 16:00:00",
    "items": [
      { "sync_id": "L-1", "vehicle_id": 2041, "fare_id": 88, "package_minutes": 60,
        "start_time": "2026-10-02 16:00:00", "expected_end_time": "2026-10-02 17:00:00",
        "base_fare": "50.00" },
      { "sync_id": "L-2", "vehicle_id": 2007, "fare_id": 88, "package_minutes": 60,
        "start_time": "2026-10-02 16:00:00", "expected_end_time": "2026-10-02 17:00:00",
        "base_fare": "50.00" }
    ],
    "payments": [
      { "sync_id": "P-1", "kind": "advance", "payment_mode_id": 1, "amount": "100.00",
        "paid_at": "2026-10-02 16:00:05" }
    ]
  }
}
```

No bill at booking: each line carries its package and agreed `base_fare`
only. Cash and card together are two entries in `payments`, each with its own
`sync_id`; at booking only `advance`.

Server checks (P1): customer, vehicles, fares and payment modes are this
company's; each vehicle is at this station, not in the order twice and not
already rented; the receipt number and every id are new. Saves everything in
one transaction with the call's history row and raises the tablet's receipt
counter to 231. Still to come (decision 7): blocked customer, vehicle
available.

```json
{ "code": "ok", "message": "Order created.",
  "data": { "sync_id": "O-1", "order_no": "DUBPP60182000231",
            "status": "active", "payment_status": "pending",
            "booked_at": "2026-10-02 16:00:05", "start_time": "2026-10-02 16:00:00",
            "completed_at": null, "cancelled_at": null,
            "customer": { "id": 5512, "name": "Ahmed Al Mansoori", "mobile": "971501234567" },
            "subtotal": null, "discount": null, "tax_percentage": null, "tax_amount": null,
            "rounding_adjustment": null, "net_amount": null,
            "amount_received": "100.00", "amount_refunded": "0.00",
            "paid_amount": "100.00", "balance_due": null, "items_out": 2,
            "items": [ { "sync_id": "L-1", "status": "active",
                         "vehicle": { "id": 2041, "name": "MO 41", "identifier": "VB1241" },
                         "start_time": "2026-10-02 16:00:00", "expected_end_time": "2026-10-02 17:00:00",
                         "end_time": null, "base_fare": "50.00", "overtime_amount": null,
                         "total_amount": null, "replaced_item_id": null, "...": "..." },
                       { "sync_id": "L-2", "...": "DC 02, the same fields" } ],
            "payments": [ { "sync_id": "P-1", "kind": "advance",
                            "payment_mode": { "id": 1, "name": "Cash" }, "amount": "100.00",
                            "paid_at": "2026-10-02 16:00:05", "...": "..." } ],
            "next_order_number": 232 } }
```

The reply is lost; the tablet resends the identical request:

```json
{ "code": "duplicate", "message": "Already recorded.", "data": { "...same data as above..." } }
```

Discount card: the operator raised a card-discount request for O-1 (existing
`/card-discounts/approval`); a manager approved 10%. It is held until
settlement.

### 3.2 Replace — MO 41 breaks at 16:20, swapped for MO 42

`POST /api/v1/operator/orders/replace`

```json
{ "request_data": {
    "sync_id": "R-1",
    "order_id": "O-1", "old_item_id": "L-1",
    "replaced_at": "2026-10-02 16:20:00",
    "reason": "Chain broken",
    "new_item": { "sync_id": "L-3", "vehicle_id": 2042, "fare_id": 88, "package_minutes": 60,
                  "start_time": "2026-10-02 16:20:00", "expected_end_time": "2026-10-02 17:00:00",
                  "base_fare": "50.00" } } }
```

In one step L-1 closes as `replaced` at 16:20 with the reason (never billed;
MO 41 is free at once) and L-3 starts on MO 42, pointing back at L-1 and
carrying the package price. Reply: the full order —

```json
{ "code": "ok", "message": "Vehicle replaced.",
  "data": { "items": [ { "sync_id": "L-1", "vehicle": { "name": "MO 41" }, "status": "replaced",
                         "end_time": "2026-10-02 16:20:00", "reason": "Chain broken" },
                       { "sync_id": "L-2", "vehicle": { "name": "DC 02" }, "status": "active" },
                       { "sync_id": "L-3", "vehicle": { "name": "MO 42" }, "status": "active",
                         "replaced_item_id": "L-1", "base_fare": "50.00" } ],
            "items_out": 2, "...": "the rest of the order" } }
```

`POST /orders/add` takes `{sync_id, order_id, added_at, item, payments?}`;
`POST /orders/remove` takes `{sync_id, order_id, item_id, removed_at, reason}`.

MO 41 is free; MO 42 is on rent. The replaced line no longer bills; the new
line keeps the package, so the bill is unchanged.

- Resend of `R-1` with a different vehicle → `409 sync_id_conflict`.
- Replacing `L-1` again with a new call id → `409 line_not_active` (final).

### 3.3 Return — one vehicle at a time

DC 02 back on time — `POST /api/v1/operator/orders/return`:

```json
{ "request_data": { "sync_id": "T-1", "order_id": "O-1", "item_id": "L-2",
                    "returned_at": "2026-10-02 17:00:00", "run_minutes": 60,
                    "overtime_amount": "0.00", "total_amount": "50.00" } }
```

The tablet works out the run time and the amounts; the server checks only
`total_amount` = `base_fare` + `overtime_amount`. Reply: the full order, the
line `returned`, `items_out: 1`.

MO 42 back 12 minutes late, AED 10 overtime:

```json
{ "request_data": { "sync_id": "T-2", "order_id": "O-1", "item_id": "L-3",
                    "returned_at": "2026-10-02 17:12:00", "run_minutes": 72,
                    "overtime_amount": "10.00", "total_amount": "60.00" } }
```

Reply: the full order, `items_out: 0`.

`items_out: 0` — everything is back; the order can be settled.

A return that reaches the server before its booking:

```json
{ "code": "order_not_synced", "message": "That order has not reached the server yet.",
  "data": { "retry": true } }
```

(409, retryable — the tablet keeps it queued.)

### 3.4 Settle — final bill, card discount, balance by card

```
subtotal     110.00   (DC 02 50.00 + MO 42 60.00; MO 41 replaced, not billed)
card disc.   -11.00   (10%, approved)
VAT 5%         4.95   (on 99.00)
rounding       0.05
net          104.00
paid         100.00   (advance P-1)
balance        4.00   → collected now by card
```

`POST /api/v1/operator/orders/settle`

```json
{ "request_data": {
    "sync_id": "S-1", "order_id": "O-1", "settled_at": "2026-10-02 17:13:30",
    "subtotal": "110.00",
    "discount": { "claim_id": "C-1", "discount_percentage": "10.00", "discount_amount": "11.00" },
    "tax_percentage": "5.00", "tax_amount": "4.95", "rounding_adjustment": "0.05", "net_amount": "104.00",
    "payments": [ { "sync_id": "P-2", "kind": "settlement", "payment_mode_id": 2, "amount": "4.00",
                    "reference_no": "4421", "paid_at": "2026-10-02 17:13:30" } ] } }
```

The tablet works out the whole bill. The server checks only that every
vehicle is back, the subtotal is the returned lines' totals, the net adds up,
and after P-2 the bill is paid in full. Then in one transaction: the bill
stored, claim C-1 Redeemed (and any other request on the order Cancelled),
payment P-2 saved, the order Completed (`paid`), and invoice
`DUBPP60182000231` issued (the order number). An automatic discount is sent as
`{card_discount_id, card_number, discount_percentage, discount_amount}` and
recorded as a new redemption.

```json
{ "code": "ok", "message": "Order settled.",
  "data": { "sync_id": "O-1", "status": "completed", "payment_status": "paid",
            "subtotal": "110.00", "net_amount": "104.00",
            "discount": { "claim_id": "C-1", "discount_percentage": "10.00", "discount_amount": "11.00" },
            "paid_amount": "104.00", "balance_due": "0.00",
            "invoice": { "invoice_no": "DUBPP60182000231", "issued_at": "2026-10-02 17:13:30",
                         "net_amount": "104.00" }, "...": "the rest of the order" } }
```

Overpaid instead (advance 110): the payment is `{"kind": "refund", "amount": "6.00"}`
and `paid_amount` still comes to 104.00.

### 3.5 Cancel — a different order, O-2

1. `POST /orders/cancel/request` `{sync_id: "C-1", order_id: "O-2", reason: "Customer changed mind"}`
   → `ok`, status `pending`.
2. A manager approves on the web screen (or the manager app).
3. `POST /orders/cancel/status` `{order_id: "O-2"}` → `approved`. Order and
   lines Cancelled, vehicles freed, a refund recorded for money taken, any
   pending card discount cancelled.
4. Cancelling a settled order → `409 order_closed` (final).

### 3.6 What the server keeps for O-1

| when (tablet) | call | what | line(s) | by |
|---|---|---|---|---|
| 16:00:05 | O-1 | booked, advance 100.00 cash | L-1, L-2 | operator on TAB-07 |
| 16:20:00 | R-1 | MO 41 → MO 42, "Chain broken" | L-1 → L-3 | operator on TAB-07 |
| 17:00:00 | T-1 | DC 02 returned | L-2 | operator on TAB-07 |
| 17:12:00 | T-2 | MO 42 returned, overtime 10.00 | L-3 | operator on TAB-07 |
| 17:13:30 | S-1 | settled 104.00, card discount 11.00, balance 4.00 card, invoice issued | — | operator on TAB-07 |

Each row also stores the request fingerprint and the reply sent.

---

## 4. Models

All in `apps/rental`. Structure only: the business validations (settlement
refusals, totals adding up, vehicle availability, …) are specified later.
Every model inherits `TimeStampedModel` (`created_by/on`, `modified_by/on`).
Money is `DecimalField(12, 2)` throughout. Times are timezone-aware;
"tablet time" fields hold when it happened on the device, `created_on` when the
server stored it.

```
Customer ─┐                         ┌─ CardDiscountClaim (apps.discount, existing)
          │                         │
        Order ──< OrderItem >── Vehicle
          │  │        └── replaced_item → OrderItem (self)
          │  ├──< Payment >── PaymentMode
          │  ├──< OrderEvent
          │  ├──< CancelRequest
          │  └──── Invoice ──< InvoiceItem
```

### 4.1 Enums

| Enum | Values |
|---|---|
| `OrderStatus` | `active`, `completed`, `cancelled` — the rental's life only |
| `PaymentStatus` | `pending` (active), `paid` (completed); blank when cancelled — computed from `status`, never set |
| `OrderItemStatus` | `active`, `returned`, `replaced`, `removed`, `cancelled` (the order was cancelled) |
| `PaymentKind` | `advance`, `settlement` (money in) · `refund` (money out) |
| `OrderAction` | `book`, `add`, `replace`, `remove`, `return`, `payment`, `settle`, `cancel_request`, `cancel_approved`, `cancel_rejected` |
| `CancelStatus` | `pending`, `approved`, `rejected` |

### 4.2 `Order` — `order`

| Field | Type | Notes |
|---|---|---|
| `id` | UUID, PK | the tablet's order `sync_id` |
| `company` | FK Company | |
| `branch` | FK Branch | the session's station |
| `device` | FK Device | the booking tablet |
| `customer` | FK Customer | |
| `customer_name`, `customer_mobile` | char | as at booking; `customer_mobile` is the full number |
| `order_no` | char(30) | the tablet's receipt number; also the invoice number |
| `status` | `OrderStatus` | default `active` |
| `is_direct_bill` | bool | return settles in one step |
| `is_hotel_order` | bool | |
| `hotel_commission` | money | |
| `booked_at` | datetime | tablet time of booking |
| `start_time` | datetime | rental start |
| `completed_at` | datetime, null | tablet time of settlement (the settle call's `settled_at`) |
| `cancelled_at` | datetime, null | when the cancel was approved |
| `subtotal` | money, null | **the bill, null until settle:** sum of billed line totals |
| `discount_claim` | one-to-one CardDiscountClaim, null | the card-discount claim applied |
| `discount_percentage` | decimal(5, 2), null | % applied, copied from the claim |
| `discount_amount` | money, null | taken off the bill |
| `tax_percentage` | decimal(5, 2), null | VAT % |
| `tax_amount` | money, null | VAT on `subtotal − discount_amount` |
| `rounding_adjustment` | decimal(6, 2), null | ± |
| `net_amount` | money, null | what the customer pays |
| `amount_received` | money, default 0 | Advance + Settlement entries; written only by the payment service |
| `amount_refunded` | money, default 0 | Refund entries; written only by the payment service |
| `paid_amount` | **generated** | `amount_received − amount_refunded` |
| `balance_due` | **generated** | `net_amount − paid_amount`; null until settle, then 0 |
| `payment_status` | **generated** | from `status`: active → `pending`, completed → `paid`, cancelled → null |

Field names follow the common shape (Shopify `subtotal_price` / `total_tax` /
`total_price`, ERPNext `net_total` / `rounding_adjustment` /
`outstanding_amount`, Square line `base_price_money`, Odoo Rental late fee into
`amount_total`). Legacy mixed rental and payment state (`OrderStatusID` 5
"Processing", `IsPaid`, `IsPaymentCompleted`); here payment status only
follows the order. "Awaiting settlement" (every vehicle back, bill not
settled) is derived from `active` + no active lines — not stored.

Keys and indexes: unique (`company`, `order_no`); index (`branch`, `status`), (`payment_status`),
(`customer`), (`device`), (`booked_at`).

Removed from today's model: `payment_mode` (per payment entry now),
`invoice_no` (the `Invoice` row), `advance_amount` (Advance entries),
`number_of_vehicles` (count of lines), `device_created_at` → `booked_at`,
`synced_at` → `created_on`.

### 4.3 `OrderItem` — `order_item` (a vehicle line)

| Field | Type | Notes |
|---|---|---|
| `id` | UUID, PK | the tablet's line `sync_id` |
| `order` | FK Order | `related_name="items"` |
| `vehicle` | FK Vehicle | |
| `status` | `OrderItemStatus` | default `active` |
| `fare` | FK Fare, null | |
| `offer` | FK Offer, null | |
| `package_minutes` | int | |
| `start_time` | datetime | |
| `expected_end_time` | datetime | start + package |
| `end_time` | datetime, null | returned / replaced / removed at (tablet time) |
| `run_minutes` | int, null | how long it actually ran; set at return (tablet) |
| `base_fare` | money | the package price agreed at booking |
| `overtime_amount` | money, null | extra-time charge; set at return (0 if on time) |
| `total_amount` | money, null | `base_fare + overtime_amount`; set at return |
| `replaced_item` | FK self, null | the old line this one replaced |
| `reason` | text | why it was replaced (on the old line) or removed — required by those calls |

Key: unique (`vehicle`) where `status = active` — a vehicle is on one active
line at a time (exists today). Index (`order`), (`vehicle`, `status`).

### 4.4 `Payment` — `payment` (payment entry)

| Field | Type | Notes |
|---|---|---|
| `id` | UUID, PK | the tablet's payment `sync_id` |
| `order` | FK Order | `related_name="payments"`; one order per entry |
| `kind` | `PaymentKind` | |
| `mode` | FK PaymentMode | |
| `amount` | money | always > 0; `kind` gives the direction |
| `reference_no` | char(50) | card slip, cheque |
| `reference_date` | date, null | |
| `paid_at` | datetime | tablet time |
| `device` | FK Device | |
| `collected_by` | FK User | |
| `remarks` | text | |

Never edited or deleted: a mistake is corrected by a reversing `refund`
entry. Index (`order`), (`paid_at`), (`mode`).

### 4.5 `OrderEvent` — `order_event` (history + idempotency)

| Field | Type | Notes |
|---|---|---|
| `id` | UUID, PK | the call's `sync_id` |
| `order` | FK Order | `related_name="events"` |
| `action` | `OrderAction` | |
| `item` | FK OrderItem, null | the line acted on (return, replace's old line, remove) |
| `new_item` | FK OrderItem, null | replace / add: the line created |
| `detail` | JSON | short summary for the history view (amounts, reason, …) |
| `request_hash` | char(64) | SHA-256 of the request body — same id, different body → conflict |
| `response` | JSON | the reply sent; replayed as-is on a duplicate |
| `happened_at` | datetime | tablet time |
| `device` | FK Device, null | null for web actions (a manager's approval) |
| `user` | FK User | |

Index (`order`, `happened_at`). Append-only.

### 4.6 `CancelRequest` — `order_cancel_request`

Same shape as `CardDiscountClaim`.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID, PK | the request's `sync_id` |
| `order` | FK Order | `related_name="cancel_requests"` |
| `status` | `CancelStatus` | default `pending` |
| `reason` | text | |
| `requested_at` | datetime | tablet time |
| `device` | FK Device | |
| `requested_by` | FK User | |
| `decided_at` | datetime, null | |
| `decided_by` | FK User, null | |
| `decision_remarks` | text | |

Key: at most one `pending` request per order (partial unique). Index
(`status`).

### 4.7 `Invoice` — `invoice`

Created at settlement, never edited.

| Field | Type | Notes |
|---|---|---|
| `id` | UUID, PK | server-made (uuid7) |
| `order` | **one-to-one** Order | `related_name="invoice"` |
| `company`, `branch` | FK | as on the order |
| `invoice_no` | char(30) | = `order.order_no` |
| `issued_at` | datetime | = settlement time |
| `device`, `issued_by` | FK | the **settling** tablet and operator |
| `company_name`, `company_trn` | char | as at issue (`Company.income_tax_number`) |
| `branch_name` | char | as at issue |
| `customer_name`, `customer_mobile` | char | as at issue |
| `subtotal`, `discount_percentage`, `discount_amount`, `tax_percentage`, `tax_amount`, `rounding_adjustment`, `net_amount` | money | copied from the order at issue |
| `payments` | JSON | breakdown at issue: `[{mode, kind, amount, reference_no}]` |

Key: unique (`company`, `invoice_no`). Index (`branch`, `issued_at`).

### 4.8 `InvoiceItem` — `invoice_item`

One row per billed line (replaced and removed lines are not billed).

| Field | Type | Notes |
|---|---|---|
| `id` | UUID, PK | server-made |
| `invoice` | FK Invoice | `related_name="items"` |
| `order_item` | FK OrderItem | traceability |
| `vehicle_identifier`, `vehicle_name`, `vehicle_type` | char | as at issue |
| `package_minutes`, `run_minutes` | int | |
| `start_time`, `end_time` | datetime | |
| `base_fare`, `overtime_amount`, `total_amount` | money | copied from the line |

### 4.9 Links to existing models

- `CardDiscountClaim.order` (exists) lists every claim raised on the order;
  `Order.discount_claim` is the one applied. Settlement redeems it and cancels
  the others; cancel approval cancels a pending one.
- `BillContinuity` (kind `order`) — each booking raises the tablet's
  `last_number` to the number in `order_no`.

---

## 5. Decisions (agreed 1 Oct 2026)

1. **Cancel needs manager approval**; operators do not cancel on their own.
   Legacy: 900 approved cancel requests (`Approve_Request`, type 4) against 137
   back-office cancels (`Cancel_Order`).
2. **Refunds are recorded by the tablet** (`/orders/payments`, kind `refund`)
   when the money is actually handed back — including after an approved cancel.
   The server never creates a payment entry on its own.
3. **Any tablet at the order's station can act on it** — return, replace,
   settle — so a rental is never stuck on a dead tablet. **Returns happen at the
   same station** only.
4. **Overtime and final amounts are computed on the tablet** and stored as sent.
5. **Card discount:** requested any time while the vehicles are out (the order
   must exist first). The tablet checks the request's status; once approved, the
   operator enters the discount from the request's details. At settle the
   tablet names the request it applied (`card_discount: {claim_id, amount}`):
   that request becomes **redeemed**, and the order stores it as
   `discount_claim` with its `discount_percentage` and `discount_amount`. Every
   other request on the order still pending, or approved but not applied,
   becomes **cancelled**. An automatic discount (no approval) is sent as
   `{card_discount_id, …}` and recorded as a new claim, redeemed at once.
6. **"On rent" is derived, never stored.** A vehicle is on rent when it has an
   active order item (one per vehicle, enforced by the database). `/vehicles`
   returns `on_rent`, the `rental` it is on (order no., expected end) and
   `can_rent` (active, available and not on rent). `is_available` stays the
   manual "can be used" switch (maintenance, held back) and is never changed
   by a rental. Legacy kept "rented" in three places (`RmsAntennaDataTracking
   .IsRented`, `RmsBranchVehicleTrackingDetails.IsOnRent`, `DMSVehicleStatus`)
   that its procedures updated inconsistently.
7. **Business validations come later** (totals adding up, vehicle and fare
   checks, settling with money owed, …). Only the guards that keep the data
   consistent are in from the start: an item must be active to be returned or
   replaced; a completed or cancelled order takes no changes (except a refund
   on a cancelled one); settle needs every vehicle back and the bill paid in
   full (`balance_not_settled`), so `paid` is always true. The tablet works out
   every amount; the basic checks are only that the figures agree: line total =
   base fare + overtime; subtotal = the returned lines' totals; net = subtotal −
   discount + VAT ± rounding (`amount_mismatch`).
8. **Every order has a customer.** **No auto-block on a low rating** (legacy
   `Service_Save_SubmitExit_Order` blocked customers rated below 2).
9. **Order number required and unique** per company; each booking raises the
   tablet's `BillContinuity.last_number` to the number used.
10. **Operator app only** for these calls; cancel approval on the web first.
11. **Not in this build:** reprint / discount / complimentary approvals, hotel
    room/guest details, loyalty points, online (customer-app) payment.

---

## 6. Legacy reference

Device calls were queued as JSON in a temp table and replayed by
`RmsTempToDBService` (`TempServiceDB.cs`): SaveOrderDetails, ExitOrder,
ReplaceOrder, EditOrder, SubmitExitOrder, ReceiveDirectOrder, SaveRequest,
SaveOrderPaymentRequest, OrderCreditNoteRequest.

| Step | Legacy proc | Notes kept / dropped |
|---|---|---|
| Book | `Save_Order_Booking` | Idempotent on (OrderNo, UserID). Direct-bill payment hardcoded to mode 1. Customer upserted by mobile. Receipt counter `+1` per upload (double-counts retries — replaced by raise-to-reported). |
| Return a vehicle | `Service_Save_Exit_Order` | Line → 2 Received, `DmsExitOrder` row, vehicle freed. |
| Settle | `Service_Save_SubmitExit_Order` | Order → 2 Received (162,429 orders). Final payment (mode hardcoded 1), deposit = collected − final, rating, auto-block < 2 (dropped), auto-close requests. Leaves lines open — 3,809 lines still Running vs 60 orders: fixed by requiring every line back first. |
| Direct rental | `Service_Receive_Direct_Order` | Order and lines → Received in one step. |
| Replace / add / remove | `Service_Save_Replace`, `Service_Save_Edit_Order` | Type 1 swap (old → 4), 2 remove (old → 3), 3 add. `DMSReplacedOrders` audit. COMMIT after CATCH (not ported). Replace never updated vehicle tracking (fixed). |
| Cancel | `Approve_Request` (type 4), `Cancel_Order` | Approval path frees vehicles but leaves payments active; back-office path deactivates payments but never frees vehicles, no transaction. Both fixed: one path doing both. |
| Payments | `DMSPayment` | All 159,511 rows mode 1 — the mode was meaningless. Online path (`APIUpdateOrderBalancePayment`) wrote `DMSOrder.PaidAmount` without a payment row. Replaced by one payment table. |
| Invoice | `RMS_PRINT_INVOICE` | Built at print time; replaced lines (status 4) excluded; 2022-11-25 cutover for "amount collected" (historic data only — transactions start fresh). |
| Credit note | `APISaveOrderCreditNoteRequest`, `DMSUpdateOrderCreditNoteStatus` | 170 orders. Not in this build. |

Approval request types (`DMSRequestType`, approved / pending): Discount
1,958 / 184 · Complimentary 1,002 / 46 · Reprint 599 / 85 · Cancel 900 / 62 ·
Bill Reprint 427 / 201 · Card Discount 58,186 / 603 (built:
`apps.discount`).

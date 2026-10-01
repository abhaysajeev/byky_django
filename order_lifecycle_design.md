# Order lifecycle — design

Status: **draft for sign-off** (2 Oct 2026). Nothing here is built yet beyond
`POST /api/v1/{app}/orders` (create), which this design replaces.

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
     *Replaced*, a new line starts. Only the current line is billed.
   - **Add** — another vehicle joins the order.
   - **Remove** — a vehicle comes off without a replacement; its line closes as
     *Cancelled*.
3. **Return** — each vehicle comes back on its own: its line becomes
   *Returned* with the real return time and its final amount (overtime
   included). The vehicle is free to rent at once.
4. **Settle** — once every vehicle is back, the bill closes:
   lines total − discounts + tax ± rounding = net. An approved card discount is
   applied and marked *Redeemed*; one still pending is *Cancelled*. Compared
   with what was paid: collect the **balance**, or record a **refund**. Order
   **Completed**; final receipt prints.
   **Direct rental** (paid in full, nothing to add): returning the vehicle
   settles the order in the same step.
5. **Cancel** — whole order only, before settlement, through manager approval.
   On approval: order and lines *Cancelled*, vehicles freed, money taken is
   recorded as a refund, a pending card discount is cancelled.

### Statuses

| Record | Flow |
|---|---|
| Order | Active → Completed · Active → Cancelled |
| Line | Active → Returned / Replaced / Cancelled |

### Money

One payment table. Every movement of money is one row: **Advance** (booking),
**Balance** (settlement), **Refund** (money back) — amount, payment mode,
reference no., tablet, operator, and the **tablet's** time. The order's paid
amount is always the sum of these rows; there is no second copy to disagree
with it (legacy's `DMSOrder.PaidAmount` vs `SUM(DMSPayment.Amount)` bug).

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

Rules for the app team: every call has a new `sync_id` and a resend reuses it;
orders and lines get their `sync_id` on the tablet; send an order's calls in
order; `ok`/`duplicate` = done, retry code = keep queued, final code = stop and
show.

Later, if tablets spend long periods offline: a batch endpoint taking the whole
queue in one request, with the same `sync_id`s and the same rules.

---

## 3. Worked example

House style: `POST` with `{credentials, request_data}`, reply
`{code, message, data}`. Ids are shortened here (`O-1`, `L-1`, …); real ones
are UUIDv7. Times are `YYYY-MM-DD HH:MM:SS` in company time.

**Scenario:** customer Ahmed at Corniche 1, tablet TAB-07 (receipt prefix
`C1`), two bikes for one hour. One breaks and is swapped; both return, one
late; the bill settles with his approved discount card.

### 3.1 Book — two bikes, AED 100 advance in cash

`POST /api/v1/operator/orders`

```json
{
  "credentials": { "...": "..." },
  "request_data": {
    "sync_id": "O-1",
    "order_no": "C1-0007-000231",
    "customer_id": 5512,
    "device_created_at": "2026-10-02 16:00:05",
    "lines": [
      { "sync_id": "L-1", "vehicle_id": 2041, "fare_id": 88, "package_minutes": 60,
        "start_time": "2026-10-02 16:00:00", "expected_end_time": "2026-10-02 17:00:00",
        "rate": "50.00", "amount": "50.00", "total_amount": "50.00" },
      { "sync_id": "L-2", "vehicle_id": 2007, "fare_id": 88, "package_minutes": 60,
        "start_time": "2026-10-02 16:00:00", "expected_end_time": "2026-10-02 17:00:00",
        "rate": "50.00", "amount": "50.00", "total_amount": "50.00" }
    ],
    "total_amount": "100.00", "tax_percentage": "5.00", "total_tax": "5.00", "net_amount": "105.00",
    "payment": { "sync_id": "P-1", "kind": "advance", "payment_mode_id": 1, "amount": "100.00",
                 "paid_at": "2026-10-02 16:00:05" }
  }
}
```

Server checks: customer exists and is not blocked; each vehicle is active,
available, at this station and not already rented; fare is this company's;
figures add up (lines 100 + tax 5 = net 105); receipt number is new for this
tablet. Saves everything in one transaction and raises the tablet's receipt
counter to 231.

```json
{ "code": "ok", "message": "Order created.",
  "data": { "sync_id": "O-1", "order_no": "C1-0007-000231", "status": "active",
            "net_amount": "105.00", "paid_amount": "100.00", "balance_due": "5.00",
            "lines": [ { "sync_id": "L-1", "vehicle": "MO 41", "status": "active" },
                       { "sync_id": "L-2", "vehicle": "DC 02", "status": "active" } ] } }
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
    "order_id": "O-1", "old_line_id": "L-1",
    "replaced_at": "2026-10-02 16:20:00",
    "reason": "Chain broken",
    "new_line": { "sync_id": "L-3", "vehicle_id": 2042, "fare_id": 88, "package_minutes": 60,
                  "start_time": "2026-10-02 16:20:00", "expected_end_time": "2026-10-02 17:00:00",
                  "rate": "50.00", "amount": "50.00", "total_amount": "50.00" } } }
```

```json
{ "code": "ok", "message": "Vehicle replaced.",
  "data": { "lines": [ { "sync_id": "L-1", "vehicle": "MO 41", "status": "replaced" },
                       { "sync_id": "L-2", "vehicle": "DC 02", "status": "active" },
                       { "sync_id": "L-3", "vehicle": "MO 42", "status": "active", "replaces": "L-1" } ],
            "net_amount": "105.00" } }
```

MO 41 is free; MO 42 is on rent. The replaced line no longer bills; the new
line keeps the package, so the bill is unchanged.

- Resend of `R-1` with a different vehicle → `409 sync_id_conflict`.
- Replacing `L-1` again with a new call id → `409 line_not_active` (final).

### 3.3 Return — one vehicle at a time

DC 02 back on time — `POST /api/v1/operator/orders/return`:

```json
{ "request_data": { "sync_id": "T-1", "order_id": "O-1", "line_id": "L-2",
                    "returned_at": "2026-10-02 17:00:00", "total_amount": "50.00" } }
```

```json
{ "code": "ok", "message": "Vehicle returned.",
  "data": { "line": { "sync_id": "L-2", "status": "returned" }, "lines_out": 1 } }
```

MO 42 back 12 minutes late, AED 10 overtime:

```json
{ "request_data": { "sync_id": "T-2", "order_id": "O-1", "line_id": "L-3",
                    "returned_at": "2026-10-02 17:12:00", "overtime_amount": "10.00",
                    "total_amount": "60.00" } }
```

```json
{ "code": "ok", "message": "Vehicle returned.",
  "data": { "line": { "sync_id": "L-3", "status": "returned" }, "lines_out": 0 } }
```

`lines_out: 0` — everything is back; the order can be settled.

A return that reaches the server before its booking:

```json
{ "code": "order_not_synced", "message": "Send the order first.", "data": {} }
```

(409, retryable — the tablet keeps it queued.)

### 3.4 Settle — final bill, card discount, balance by card

```
lines        110.00   (DC 02 50.00 + MO 42 60.00; MO 41 replaced, not billed)
card disc.   -11.00   (10%, approved)
subtotal      99.00
tax 5%         4.95
rounding       0.05
net          104.00
paid         100.00   (advance P-1)
balance        4.00   → collected now by card
```

`POST /api/v1/operator/orders/settle`

```json
{ "request_data": {
    "sync_id": "S-1", "order_id": "O-1", "settled_at": "2026-10-02 17:13:30",
    "total_amount": "110.00", "card_discount_amount": "11.00", "total_tax": "4.95",
    "rounded_diff": "0.05", "net_amount": "104.00",
    "payments": [ { "sync_id": "P-2", "kind": "balance", "payment_mode_id": 2, "amount": "4.00",
                    "reference_no": "4421", "paid_at": "2026-10-02 17:13:30" } ] } }
```

Server checks the figures add up and every line is back; then in one
transaction: order Completed, payment P-2 saved, the card-discount claim
marked Redeemed with AED 11.00. Had the claim still been pending, it would be
Cancelled and the tablet would have billed without it.

```json
{ "code": "ok", "message": "Order settled.",
  "data": { "sync_id": "O-1", "status": "completed", "net_amount": "104.00",
            "paid_amount": "104.00", "balance_due": "0.00", "card_discount": "redeemed" } }
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
| 17:13:30 | S-1 | settled 104.00, card discount 11.00, balance 4.00 card | — | operator on TAB-07 |

Each row also stores the request fingerprint and the reply sent.

---

## 4. Assumptions (to confirm)

1. **Cancel needs manager approval** (as the card discount does); operators do
   not cancel on their own. Legacy: 900 approved cancel requests
   (`Approve_Request`, type 4) against 137 back-office cancels (`Cancel_Order`).
2. **Overtime and final amounts are computed on the tablet** and trusted, as
   booking is today; the server checks only that totals add up.
3. **Every order has a customer** — no anonymous walk-ins.
4. **No auto-block on a low rating** (legacy `Service_Save_SubmitExit_Order`
   blocked customers rated below 2).
5. **Receipt number required and unique per tablet**; each order raises the
   tablet's `BillContinuity.last_number` to the number used.
6. **Not in this build:** reprint / discount / complimentary approvals, hotel
   room/guest details, loyalty points, online (customer-app) payment.

---

## 5. Legacy reference

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

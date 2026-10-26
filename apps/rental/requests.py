"""Requests on orders: an operator asks, a manager decides -- on the web or in
the manager app.

Ported from legacy DMSRequests (Service_Save_RequestApproval, Approve_Request,
Reject_Request, Service_Get_Request_Approval_By_OrderNo,
Service_Get_Bill_Reprint_Details, RMSUpdateOrderComplimentary,
Service_Save_SubmitExit_Order). Kept: the tablet asks against an order; the
approval for a discount is a rule (DiscountType + value) the tablet applies at
settle; a complimentary ride settles at net 0; a reprint approval lets the
tablet print a settled bill again; a request still waiting at settle is
closed. Not kept: the multi-device IsIgnored patch -- one pending or approved
request per order and kind is a database rule instead; legacy's web-only
approval -- the manager app decides too, and where each decision was made is
recorded; and a complimentary ride's payments deleted -- an advance goes back
as refund entries, so the payment history stays whole.

Rules agreed with the owner (8 Oct 2026):
- `discount` and `complimentary` -- a running order; `reprint` -- a settled
  one. Any tablet at the order's station may ask or withdraw.
- Rejected, withdrawn, revoked and used requests leave room for a new one.
- A manager discount, a complimentary and a card discount never share an order.
- A discount or complimentary approval is revoked only while the order runs; a
  reprint is approved or rejected, never revoked.
- The settle applies an approval exactly: the same %, the AED amount (capped
  at the subtotal), or the whole subtotal for a complimentary ride.
- One reprint per approval: the tablet reports it printed (`used`).
"""

import datetime
from decimal import Decimal, InvalidOperation

from django.core.paginator import Paginator
from django.db import transaction
from django.utils import timezone

from apps.discount.models import CardDiscountClaim, ClaimStatus
from apps.notifications import services as notifications
from apps.notifications.models import Action
from apps.photos import services as photo_services
from apps.rental import live
from apps.rental.models import (
    REQUEST_LIVE,
    DecisionChannel,
    DiscountType,
    Invoice,
    Order,
    OrderAction,
    OrderItemStatus,
    OrderRequest,
    OrderRequestKind,
    OrderRequestStatus,
    OrderStatus,
)
from apps.rental.services import OrderRefused, _open_order, lock_order, run_once
from core.enums import Channel
from core.timezones import zone_for

__all__ = ["DecisionChannel"]      # the manager API and the web name channels through this module

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
PAGE_SIZE = 50

# Card-discount claims that hold an order against a manager discount or a complimentary.
CARD_CLAIM_LIVE = (ClaimStatus.PENDING, ClaimStatus.APPROVED)

# The kinds that take money off a running order's bill -- at most one per order.
BILL_KINDS = (OrderRequestKind.DISCOUNT, OrderRequestKind.COMPLIMENTARY)
# Revocable while the order runs.
REVOCABLE = BILL_KINDS
# Printed once when approved, then reported with orders/requests/used.
REPRINT_KINDS = (OrderRequestKind.REPRINT, OrderRequestKind.ORDER_REPRINT)

# (pending, approved) refusal per kind; and the refusal another bill kind gets.
_LIVE_REFUSALS = {
    OrderRequestKind.DISCOUNT: ("discount_request_pending", "discount_approved"),
    OrderRequestKind.COMPLIMENTARY: ("complimentary_request_pending", "complimentary_approved"),
    OrderRequestKind.REPRINT: ("reprint_request_pending", "reprint_approved"),
    OrderRequestKind.ORDER_REPRINT: ("order_reprint_request_pending", "order_reprint_approved"),
    OrderRequestKind.CANCEL: ("cancel_request_pending", "cancel_approved"),
}
_HELD_BY = {
    OrderRequestKind.DISCOUNT: "discount_requested",
    OrderRequestKind.COMPLIMENTARY: "complimentary_requested",
}


class RequestRefused(Exception):
    """A manager's or the web's action that cannot be done. `message` is shown
    as it is; `status` is the HTTP status the manager API answers with."""

    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _local(moment, zone):
    return moment.astimezone(zone).strftime(TIME_FORMAT) if moment else None


def _name(user):
    if user is None:
        return None
    return user.display_name or user.username


def _value(req):
    if req.discount_value is None:
        return None
    if req.discount_type == DiscountType.PERCENT:
        return str(req.discount_value.quantize(Decimal("0.01")))
    return str(req.discount_value)


def request_json(req, zone):
    """One request as the tablet sees it."""
    return {
        "request_sync_id": str(req.pk),
        "order_id": str(req.order_id),
        "order_no": req.order.order_no,
        "kind": req.kind,
        "status": req.status,
        "reason": req.reason,
        "requested_at": _local(req.requested_at, zone),
        "discount_type": req.discount_type or None,
        "discount_value": _value(req),
        "decided_by": _name(req.decided_by),
        "decided_channel": req.decided_channel or None,
        "decided_at": _local(req.decided_at, zone),
        "decision_note": req.decision_note,
        "revoked_by": _name(req.revoked_by),
        "revoked_channel": req.revoked_channel or None,
        "revoked_at": _local(req.revoked_at, zone),
        "revoke_note": req.revoke_note,
        "applied_amount": str(req.applied_amount) if req.applied_amount is not None else None,
        "applied_at": _local(req.applied_at, zone),
        "used_at": _local(req.used_at, zone),
    }


# -- Tablet --------------------------------------------------------------------------


def _refuse_if_held(order, kind):
    """The refusal when the order already has a live request of this kind, or
    -- for a bill kind -- another bill kind or a card discount."""
    live = {req.kind: req for req in order.requests.filter(status__in=REQUEST_LIVE)}
    if kind in live:
        pending, approved = _LIVE_REFUSALS[kind]
        raise OrderRefused(pending if live[kind].status == OrderRequestStatus.PENDING else approved)
    if kind not in BILL_KINDS:
        return
    for other in BILL_KINDS:
        if other != kind and other in live:
            raise OrderRefused(_HELD_BY[other])
    if CardDiscountClaim.objects.filter(order=order, status__in=CARD_CLAIM_LIVE).exists():
        raise OrderRefused("card_discount_requested")


def create_request(session, values, request_data):
    """A tablet asks a manager about an order at its station: a discount or a
    complimentary on a running order, a reprint of a settled one. Returns
    (reply, done_now). The call's sync_id becomes the request's id."""
    company = session.branch.company
    zone = zone_for(company)
    kind = values["kind"]

    def apply():
        if kind == OrderRequestKind.REPRINT:
            order = lock_order(values["order_id"], session.branch)
            if order.status != OrderStatus.COMPLETED:
                raise OrderRefused("order_not_settled", "A reprint needs a settled order.")
        elif kind == OrderRequestKind.ORDER_REPRINT:
            # The receipt printed at booking: while the order runs or after it
            # settled -- never once it is cancelled.
            order = lock_order(values["order_id"], session.branch)
            if order.status == OrderStatus.CANCELLED:
                raise OrderRefused("order_closed", "A cancelled order's receipt is not printed again.")
        else:
            order = _open_order(session, values["order_id"])
        _refuse_if_held(order, kind)
        req = OrderRequest.objects.create(
            id=values["sync_id"], kind=kind, company=company, branch=order.branch,
            order=order, status=OrderRequestStatus.PENDING, device=session.device, requested_by=session.user,
            requested_at=values["requested_at"], reason=(values.get("reason") or "").strip(),
            created_by=session.user, modified_by=session.user,
        )
        notifications.notify(req, Action.RAISED)
        event = {
            "action": OrderAction.REQUEST, "happened_at": values["requested_at"],
            "device": session.device, "user": session.user,
            "detail": {"request_sync_id": str(req.pk), "kind": req.kind},
        }
        return order, event, request_json(req, zone)

    return run_once(event_id=values["sync_id"], company=company, request_data=request_data, apply=apply)


def _own_request(session, request_sync_id):
    """(order, request) -- the request at the session's station, both locked."""
    found = OrderRequest.objects.filter(pk=request_sync_id, branch=session.branch).first()
    if found is None:
        raise OrderRefused("unknown_request")
    order = lock_order(found.order_id, session.branch)
    return order, OrderRequest.objects.select_for_update().get(pk=found.pk)


def withdraw(session, values, request_data):
    """A tablet takes back a request still waiting. Decided or closed ones
    stay as they are (request_closed); withdrawing twice answers the same."""
    company = session.branch.company
    zone = zone_for(company)

    def apply():
        order, req = _own_request(session, values["request_sync_id"])
        if req.status == OrderRequestStatus.PENDING:
            req.status, req.closed_at = OrderRequestStatus.WITHDRAWN, values["withdrawn_at"]
            req.modified_by = session.user
            req.save()
            notifications.notify(req, Action.WITHDRAWN)
        elif req.status != OrderRequestStatus.WITHDRAWN:
            raise OrderRefused("request_closed")
        event = {
            "action": OrderAction.REQUEST_WITHDRAW, "happened_at": values["withdrawn_at"],
            "device": session.device, "user": session.user,
            "detail": {"request_sync_id": str(req.pk), "kind": req.kind},
        }
        return order, event, request_json(req, zone)

    return run_once(event_id=values["sync_id"], company=company, request_data=request_data, apply=apply)


def mark_used(session, values, request_data):
    """The tablet printed the bill again under an approved reprint: the
    approval is spent (`used`). Another reprint needs a new request."""
    company = session.branch.company
    zone = zone_for(company)

    def apply():
        order, req = _own_request(session, values["request_sync_id"])
        if req.kind not in REPRINT_KINDS:
            raise OrderRefused("unknown_request", "No reprint request with that id at this station.")
        if req.status == OrderRequestStatus.USED:
            raise OrderRefused("request_used")
        if req.status != OrderRequestStatus.APPROVED:
            raise OrderRefused("request_not_approved")
        req.status, req.used_at, req.modified_by = OrderRequestStatus.USED, values["used_at"], session.user
        req.save()
        event = {
            "action": OrderAction.REQUEST_USED, "happened_at": values["used_at"],
            "device": session.device, "user": session.user,
            "detail": {"request_sync_id": str(req.pk), "kind": req.kind},
        }
        return order, event, request_json(req, zone)

    return run_once(event_id=values["sync_id"], company=company, request_data=request_data, apply=apply)


def _requested_between(rows, zone, from_date=None, to_date=None):
    """Requests asked on or after `from_date` and on or before `to_date`
    (company-local days, both included); either may be left out."""
    if from_date:
        rows = rows.filter(requested_at__gte=datetime.datetime.combine(from_date, datetime.time.min, tzinfo=zone))
    if to_date:
        rows = rows.filter(requested_at__lt=datetime.datetime.combine(
            to_date + datetime.timedelta(days=1), datetime.time.min, tzinfo=zone))
    return rows


# The operator's Requests screen lists everything the station sent for a
# decision, in one list: order requests, credit-note requests, and card
# discounts that needed approval. Listing only -- each kind's own status call
# (orders/requests/status, orders/credit-notes/status,
# card-discounts/approval/status) stays the way to follow one.
CREDIT_NOTE, CARD_DISCOUNT = "credit_note", "card_discount"
# A vehicle or station asset reported for service (apps/fleet/service_requests.py):
# in the manager's list only -- the tablet follows its reports with
# service-requests/list.
SERVICE = "service"
LIST_KINDS = (*OrderRequestKind.values, CREDIT_NOTE, CARD_DISCOUNT)


def _station_sources(branch, kind, device=None):
    """(source, queryset) for each kind of request the list may show -- the
    station's, or only `device`'s when one is given (`mine`)."""
    from apps.rental.models import CreditNote, CreditNoteSource

    sources = []
    if kind is None or kind in OrderRequestKind.values:
        requests = OrderRequest.objects.filter(branch=branch)
        requests = requests.filter(kind=kind) if kind else requests
        sources.append(("request", requests.filter(device=device) if device else requests))
    if kind in (None, CREDIT_NOTE):
        # Only what a tablet asked for; one issued straight from the web was
        # never sent for a decision.
        notes = CreditNote.objects.filter(branch=branch, source=CreditNoteSource.DEVICE)
        sources.append((CREDIT_NOTE, notes.filter(device=device) if device else notes))
    if kind in (None, CARD_DISCOUNT):
        # Only claims that went to someone for approval. A claim keeps the
        # tablet's installation id, not the device row: after a reinstall the
        # tablet's earlier claims no longer count as its own.
        claims = CardDiscountClaim.objects.filter(branch=branch, requires_approval=True)
        sources.append((CARD_DISCOUNT, claims.filter(rms_installation_id=device.installation_id)
                        if device else claims))
    return sources


_PENDING = {"request": OrderRequestStatus.PENDING, CREDIT_NOTE: "pending", CARD_DISCOUNT: ClaimStatus.PENDING,
            SERVICE: "pending"}


def station_list(branch, *, pending_only=False, kind=None, from_date=None, to_date=None, page=1, device=None):
    """(rows, page) of one station's requests of every kind, newest first --
    the operator app's Requests screen. Sorted and paged together in the
    database (one UNION of id, time and source), then each page's rows loaded
    from their own table."""
    from django.db.models import CharField, Value

    zone = zone_for(branch.company)
    keyed = []
    for source, rows in _station_sources(branch, kind, device):
        rows = _requested_between(rows, zone, from_date, to_date)
        if pending_only:
            rows = rows.filter(status=_PENDING[source])
        keyed.append(rows.annotate(list_source=Value(source, output_field=CharField()))
                     .values_list("pk", "requested_at", "list_source").order_by())
    union = keyed[0].union(*keyed[1:], all=True).order_by("-requested_at", "-pk") if len(keyed) > 1 \
        else keyed[0].order_by("-requested_at", "-pk")
    page = Paginator(union, PAGE_SIZE).get_page(page)
    return _station_rows(list(page.object_list), zone), page


def _station_rows(keys, zone):
    from apps.rental.models import CreditNote

    wanted = {}
    for pk, _, source in keys:
        wanted.setdefault(source, []).append(pk)
    loaded = {}
    if "request" in wanted:
        for req in (OrderRequest.objects.filter(pk__in=wanted["request"])
                    .select_related("order", "decided_by", "revoked_by")):
            loaded[("request", req.pk)] = {**request_json(req, zone), "customer_name": req.order.customer_name,
                                           "details": request_details(req)}
    if CREDIT_NOTE in wanted:
        for note in CreditNote.objects.filter(pk__in=wanted[CREDIT_NOTE]).select_related("order", "decided_by"):
            loaded[(CREDIT_NOTE, note.pk)] = _credit_note_row(note, zone)
    if CARD_DISCOUNT in wanted:
        for claim in (CardDiscountClaim.objects.filter(pk__in=wanted[CARD_DISCOUNT])
                      .select_related("order", "decided_by", "card_type", "card_grade")):
            loaded[(CARD_DISCOUNT, claim.pk)] = _claim_row(claim, zone)
    return [loaded[(source, pk)] for pk, _, source in keys]


def _other_row(kind, pk, order, zone, *, status, reason, requested_at, decided_by, decided_channel, decided_at,
               note, details):
    """A credit note or card-discount request in the same row shape as an
    order request: the request-only fields are empty, and `details` carries
    what is its own."""
    return {
        "request_sync_id": str(pk), "order_id": str(order.pk), "order_no": order.order_no,
        "kind": kind, "status": status, "reason": reason, "requested_at": _local(requested_at, zone),
        "discount_type": None, "discount_value": None,
        "decided_by": _name(decided_by), "decided_channel": decided_channel or None,
        "decided_at": _local(decided_at, zone), "decision_note": note,
        "revoked_by": None, "revoked_channel": None, "revoked_at": None, "revoke_note": "",
        "applied_amount": None, "applied_at": None, "used_at": None,
        "customer_name": order.customer_name,
        "details": details,
    }


def _credit_note_row(note, zone):
    approved = note.status == "approved"
    return _other_row(
        CREDIT_NOTE, note.pk, note.order, zone, status=note.status, reason=note.reason,
        requested_at=note.requested_at, decided_by=note.decided_by, decided_channel=note.decided_channel,
        decided_at=note.decided_at, note=note.decision_remark,
        details={"credit_note_no": note.credit_note_no or None,
                 "net_amount": str(note.net_amount) if approved else None},
    )


def _claim_row(claim, zone):
    return _other_row(
        CARD_DISCOUNT, claim.pk, claim.order, zone, status=claim.status, reason="",
        requested_at=claim.requested_at, decided_by=claim.decided_by, decided_channel=claim.decided_channel,
        decided_at=claim.decided_at, note=claim.remarks,
        details={"card_type": claim.card_type.name, "card_grade": claim.card_grade.name,
                 "card_number": claim.card_number, "discount_percent": str(claim.discount_percent),
                 "bill_amount": _money(claim.bill_amount), "discount_amount": _money(claim.discount_amount),
                 "net_amount": _money(claim.net_amount)},
    )


def requests_for_orders(company, order_ids):
    """Every request on these orders of the company, newest first."""
    return list(
        OrderRequest.objects.filter(company=company, order_id__in=order_ids)
        .select_related("order", "decided_by", "revoked_by").order_by("-requested_at")
    )


# -- Manager: web and manager app --------------------------------------------------


def parse_rule(discount_type, value):
    """(type, Decimal value) of a discount approval, or RequestRefused: a
    percentage above 0 and at most 100 with 2 decimals; an AED amount above 0
    with 3."""
    if discount_type not in DiscountType.values:
        raise RequestRefused("invalid_request", "Choose a percentage or an amount.", 400)
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise RequestRefused("invalid_request", "Enter the discount value.", 400) from None
    if not number.is_finite() or number <= 0:
        raise RequestRefused("invalid_request", "The discount must be more than 0.", 400)
    places = max(-number.normalize().as_tuple().exponent, 0)      # 12.500 is 2 places short of 3
    if discount_type == DiscountType.PERCENT:
        if number > 100:
            raise RequestRefused("invalid_request", "A percentage cannot be more than 100.", 400)
        if places > 2:
            raise RequestRefused("invalid_request", "Use at most two decimal places for a percentage.", 400)
    else:
        if places > 3:
            raise RequestRefused("invalid_request", "Use at most three decimal places for an amount.", 400)
        if number >= Decimal("10000000000"):
            raise RequestRefused("invalid_request", "That amount is too large.", 400)
    return discount_type, number


def _locked(request_id, company_ids):
    """(order, request), both row-locked -- the order first, the way the settle
    takes them, so a decision and a settle on one order take turns."""
    found = OrderRequest.objects.filter(pk=request_id, company_id__in=company_ids).first()
    if found is None:
        raise RequestRefused("unknown_request", "No request with that id.", 404)
    order = Order.objects.select_for_update().get(pk=found.order_id)
    return order, OrderRequest.objects.select_for_update().get(pk=found.pk)


def _already(req):
    return RequestRefused("request_decided", f"This request is already {req.get_status_display().lower()}.")


def _order_open_for(req, order):
    """A bill request needs the order still running; an invoice reprint a
    settled one; an order receipt reprint any order not cancelled."""
    if req.kind == OrderRequestKind.REPRINT:
        return order.status == OrderStatus.COMPLETED
    if req.kind == OrderRequestKind.ORDER_REPRINT:
        return order.status != OrderStatus.CANCELLED
    return order.status == OrderStatus.ACTIVE


def approve(user, request_id, company_ids, channel, discount_type=None, value=None, note=""):
    """Approve a pending request. A discount takes its rule; a complimentary or
    a reprint takes none."""
    with transaction.atomic():
        order, req = _locked(request_id, company_ids)
        if req.status != OrderRequestStatus.PENDING:
            raise _already(req)
        if not _order_open_for(req, order):
            raise RequestRefused("order_closed", "This order is already completed or cancelled.")
        if req.kind == OrderRequestKind.DISCOUNT:
            req.discount_type, req.discount_value = parse_rule(discount_type, value)
        now = timezone.now()
        req.status = OrderRequestStatus.APPROVED
        req.decided_by, req.decided_channel, req.decided_at = user, channel, now
        req.decision_note, req.modified_by = (note or "").strip(), user
        req.save()
        notifications.notify(req, Action.APPROVED)
        if req.kind == OrderRequestKind.CANCEL:
            cancel_order(order, req, user, now)
    return req


def cancel_order(order, cancel, user, now):
    """An approved cancel takes effect, inside the approval's transaction (the
    order is already locked): the order and its running lines are cancelled,
    so their vehicles can be rented again at once; returned, replaced and
    removed lines stay as they are. Requests still waiting on the order are
    closed; an approved discount or complimentary stays approved but can never
    be used -- nothing settles a cancelled order. Card-discount claims are
    cancelled, so the customer's card usage is not spent. Money already taken
    stays recorded; a refund may be added with orders/payments."""
    from apps.discount.services import cancel_unused_for_order

    order.status, order.cancelled_at, order.modified_by = OrderStatus.CANCELLED, now, user
    order.save(update_fields=["status", "cancelled_at", "modified_by", "modified_on"])
    order.items.filter(status=OrderItemStatus.ACTIVE).update(status=OrderItemStatus.CANCELLED, modified_on=now)
    waiting = order.requests.filter(status=OrderRequestStatus.PENDING).exclude(pk=cancel.pk)
    closing = list(waiting.select_related("branch"))
    waiting.update(status=OrderRequestStatus.CLOSED, closed_at=now, modified_on=now)
    notifications.notify_all(closing, Action.CLOSED)
    cancel_unused_for_order(order, now=now)
    # A web or manager cancel writes no OrderEvent, so it is not seen by
    # services.run_once: tell Live Monitoring here.
    live.order_changed(order)


def cancel_directly(user, order_id, company_ids, reason):
    """The back office cancels a running order on the web, with no tablet
    request. Recorded as a cancel request asked and approved by the same
    person on the web, so both routes leave one shape of record."""
    from core.ids import uuid7

    reason = (reason or "").strip()
    if not reason:
        raise RequestRefused("invalid_request", "Give the reason for the cancel.", 400)
    with transaction.atomic():
        order = Order.objects.select_for_update().filter(pk=order_id, company_id__in=company_ids).first()
        if order is None:
            raise RequestRefused("unknown_order", "No such order.", 404)
        if order.status != OrderStatus.ACTIVE:
            raise RequestRefused("order_closed", "Only a running order can be cancelled.")
        if order.requests.filter(kind=OrderRequestKind.CANCEL, status__in=REQUEST_LIVE).exists():
            raise RequestRefused("cancel_request_pending",
                                 "A cancel request is waiting for this order -- approve or reject it instead.")
        now = timezone.now()
        req = OrderRequest.objects.create(
            id=uuid7(), kind=OrderRequestKind.CANCEL, company_id=order.company_id, branch_id=order.branch_id,
            order=order, status=OrderRequestStatus.APPROVED, device=None, requested_by=user, requested_at=now,
            reason=reason, decided_by=user, decided_channel=DecisionChannel.WEB, decided_at=now,
            created_by=user, modified_by=user,
        )
        cancel_order(order, req, user, now)
    return req


def reject(user, request_id, company_ids, channel, note=""):
    with transaction.atomic():
        _, req = _locked(request_id, company_ids)
        if req.status != OrderRequestStatus.PENDING:
            raise _already(req)
        req.status = OrderRequestStatus.REJECTED
        req.decided_by, req.decided_channel, req.decided_at = user, channel, timezone.now()
        req.decision_note, req.modified_by = (note or "").strip(), user
        req.save()
        notifications.notify(req, Action.REJECTED)
    return req


def revoke(user, request_id, company_ids, channel, note=""):
    """Take a discount or complimentary approval back while the order still
    runs. The tablet sees `revoked` in status, and the settle then refuses it."""
    with transaction.atomic():
        order, req = _locked(request_id, company_ids)
        if req.kind not in REVOCABLE:
            raise RequestRefused("request_not_revocable",
                                 f"A {req.get_kind_display().lower()} is approved or rejected, not revoked.")
        if req.status != OrderRequestStatus.APPROVED:
            raise RequestRefused("request_not_approved", "Only an approved request can be revoked.")
        if order.status != OrderStatus.ACTIVE:
            raise RequestRefused("order_closed", "This order is already completed or cancelled.")
        req.status = OrderRequestStatus.REVOKED
        req.revoked_by, req.revoked_channel, req.revoked_at = user, channel, timezone.now()
        req.revoke_note, req.modified_by = (note or "").strip(), user
        req.save()
        notifications.notify(req, Action.REVOKED)
    return req


def _lines(order, zone, now):
    """The order's billable vehicles: out now, or back."""
    rows = []
    for item in order.items.all():
        if item.status not in (OrderItemStatus.ACTIVE, OrderItemStatus.RETURNED):
            continue
        end = item.end_time or now
        rows.append({
            "vehicle": item.vehicle.vehicle_name, "identifier": item.vehicle.identifier,
            "vehicle_type": item.vehicle.vehicle_type.vehicle_type_name, "status": item.status,
            "start_time": _local(item.start_time, zone), "expected_end_time": _local(item.expected_end_time, zone),
            "end_time": _local(item.end_time, zone),
            "minutes_run": max(int((end - item.start_time).total_seconds() // 60), 0),
        })
    return rows


def _money(value):
    return str(value) if value is not None else None


def _order_block(order, zone, now):
    """The order a manager decides about: customer, advance, every vehicle."""
    return {
        "order_no": order.order_no, "status": order.status,
        "customer_name": order.customer_name, "customer_mobile": order.customer_mobile,
        "booked_at": _local(order.booked_at, zone),
        "advance_paid": str(order.paid_amount),
        "net_amount": _money(order.net_amount),
        "vehicles": _lines(order, zone, now),
    }


def manager_json(req, zone, now):
    """One request as the manager app sees it: the tablet's view plus the
    order it is about -- and, once settled, its bill."""
    return {
        **request_json(req, zone),
        "branch_id": req.branch_id,
        "station": req.branch.name,
        "requested_by": _name(req.requested_by),
        "order": _order_block(req.order, zone, now),
    }


def _manager_rows():
    return (OrderRequest.objects
            .select_related("order", "branch", "company", "requested_by", "decided_by", "revoked_by")
            .prefetch_related("order__items__vehicle__vehicle_type"))


def manager_row(request_id):
    """One request, loaded for manager_json."""
    return _manager_rows().get(pk=request_id)


def request_details(req):
    """`details` of an order-request row in the lists: the invoice an invoice
    reprint is for; nothing for the other kinds (the row carries the order)."""
    if req.kind != OrderRequestKind.REPRINT:
        return None
    invoice = Invoice.objects.filter(order_id=req.order_id).only("invoice_no", "net_amount").first()
    if invoice is None:
        return None
    return {"invoice_no": invoice.invoice_no, "invoice_net_amount": _money(invoice.net_amount)}


def _company_sources(company_ids, kind, state_id, branch_id):
    """(source, queryset) per kind the manager's list may show, across the
    companies -- the same three as the operator's (_station_sources)."""
    from apps.rental.models import CreditNote, CreditNoteSource

    def where(rows):
        rows = rows.filter(company_id__in=company_ids)
        if state_id:
            rows = rows.filter(branch__location__state_id=state_id)
        return rows.filter(branch_id=branch_id) if branch_id else rows

    sources = []
    if kind is None or kind in OrderRequestKind.values:
        requests = where(OrderRequest.objects.all())
        sources.append(("request", requests.filter(kind=kind) if kind else requests))
    if kind in (None, CREDIT_NOTE):
        sources.append((CREDIT_NOTE, where(CreditNote.objects.filter(source=CreditNoteSource.DEVICE))))
    if kind in (None, CARD_DISCOUNT):
        sources.append((CARD_DISCOUNT, where(CardDiscountClaim.objects.filter(requires_approval=True))))
    if kind in (None, SERVICE):
        from apps.fleet.models import ServiceRequest

        sources.append((SERVICE, where(ServiceRequest.objects.all())))
    return sources


def manager_list(company_ids, *, pending_only=False, kind=None, state_id=None, branch_id=None, from_date=None,
                 to_date=None, zone=None, page=1):
    """(rows, page) of every kind sent for a decision across the companies --
    order requests, credit-note requests, card discounts that need approval --
    newest first, sorted and paged together (one UNION, as station_list).
    Listing only: each kind is decided with its own approve / reject call."""
    from django.db.models import CharField, Value

    keyed = []
    for source, rows in _company_sources(company_ids, kind, state_id, branch_id):
        if zone is not None:
            rows = _requested_between(rows, zone, from_date, to_date)
        if pending_only:
            rows = rows.filter(status=_PENDING[source])
        keyed.append(rows.annotate(list_source=Value(source, output_field=CharField()))
                     .values_list("pk", "requested_at", "list_source").order_by())
    union = keyed[0].union(*keyed[1:], all=True).order_by("-requested_at", "-pk") if len(keyed) > 1 \
        else keyed[0].order_by("-requested_at", "-pk")
    page = Paginator(union, PAGE_SIZE).get_page(page)
    return _manager_rows_of(list(page.object_list)), page


def _manager_rows_of(keys):
    from apps.rental.models import CreditNote

    wanted = {}
    for pk, _, source in keys:
        wanted.setdefault(source, []).append(pk)
    now, zones, loaded = timezone.now(), {}, {}

    def zone_of(row):
        return zones.setdefault(row.company_id, zone_for(row.company))

    orders = "order__items__vehicle__vehicle_type"
    if "request" in wanted:
        for req in _manager_rows().filter(pk__in=wanted["request"]):
            loaded[("request", req.pk)] = {**manager_json(req, zone_of(req), now), "details": request_details(req)}
    if CREDIT_NOTE in wanted:
        for note in (CreditNote.objects.filter(pk__in=wanted[CREDIT_NOTE])
                     .select_related("order", "invoice", "branch", "company", "requested_by", "decided_by")
                     .prefetch_related(orders)):
            zone = zone_of(note)
            loaded[(CREDIT_NOTE, note.pk)] = _manager_other(_credit_note_row(note, zone), note, zone, now, details={
                "credit_note_no": note.credit_note_no or None,
                "invoice_no": note.invoice.invoice_no, "invoice_net_amount": _money(note.invoice.net_amount),
                "net_amount": _money(note.net_amount) if note.status == "approved" else None,
                "tax_amount": _money(note.tax_amount) if note.status == "approved" else None,
            })
    if CARD_DISCOUNT in wanted:
        for claim in (CardDiscountClaim.objects.filter(pk__in=wanted[CARD_DISCOUNT])
                      .select_related("order", "branch", "company", "requested_by", "decided_by", "card_type",
                                      "card_grade")
                      .prefetch_related(orders)):
            zone = zone_of(claim)
            row = _claim_row(claim, zone)
            loaded[(CARD_DISCOUNT, claim.pk)] = _manager_other(row, claim, zone, now, details={
                **row["details"], "card_photo": claim.card_photo or None,
                **card_photo_fields(claim), "fare_basis": claim.fare_basis,
            })
    if SERVICE in wanted:
        from apps.fleet import service_requests
        from apps.fleet.models import ServiceRequest

        for req in service_requests.with_rows(ServiceRequest.objects.filter(pk__in=wanted[SERVICE])):
            loaded[(SERVICE, req.pk)] = service_requests.manager_json(req, zone_of(req))
    return [loaded[(source, pk)] for pk, _, source in keys]


def card_photo_fields(claim):
    """The uploaded card photo (apps/photos) for the manager app: its id and
    two URLs relative to the server, which need the access token."""
    if not claim.photo_id:
        return {"card_photo_id": None, "card_photo_url": None, "card_photo_thumb_url": None}
    return {
        "card_photo_id": str(claim.photo_id),
        "card_photo_url": photo_services.view_path(Channel.MANAGER, claim.photo_id, "display"),
        "card_photo_thumb_url": photo_services.view_path(Channel.MANAGER, claim.photo_id, "thumb"),
    }


def _manager_other(row, record, zone, now, *, details):
    """A credit note or card-discount request in the manager's row shape: the
    tablet-shaped row, plus the station, who asked, and the order."""
    row = {k: v for k, v in row.items() if k != "customer_name"}
    row.update({
        "branch_id": record.branch_id, "station": record.branch.name if record.branch_id else None,
        "requested_by": _name(record.requested_by), "order": _order_block(record.order, zone, now),
        "details": details,
    })
    return row


# -- At settle (apps/rental/services.py::settle_order) ------------------------------


def expected_discount(req, subtotal):
    """What the settle must carry for this approval: (field, value)."""
    if req.kind == OrderRequestKind.COMPLIMENTARY:
        return "discount_amount", subtotal
    if req.discount_type == DiscountType.PERCENT:
        return "discount_percentage", req.discount_value.quantize(Decimal("0.01"))
    return "discount_amount", min(req.discount_value, subtotal)


_NOT_APPLIED = {
    OrderRequestKind.DISCOUNT: ("approved_discount_not_applied", "manager_discount"),
    OrderRequestKind.COMPLIMENTARY: ("approved_complimentary_not_applied", "complimentary"),
}
_BLOCK_REFUSALS = {
    OrderRequestKind.DISCOUNT: ("unknown_discount_request", "discount_request_not_approved"),
    OrderRequestKind.COMPLIMENTARY: ("unknown_complimentary_request", "complimentary_request_not_approved"),
}


def check_at_settle(order, values):
    """The approved discount or complimentary the settle applies, or None.

    Refused when a block names a request that is not this order's approved one
    of that kind, carries other figures than the approval, or is missing while
    an approval waits. A complimentary bill carries no VAT."""
    blocks = {OrderRequestKind.DISCOUNT: values.get("manager_discount"),
              OrderRequestKind.COMPLIMENTARY: values.get("complimentary")}
    approved = (order.requests.select_for_update()
                .filter(kind__in=BILL_KINDS, status=OrderRequestStatus.APPROVED).first())
    if approved is not None and not blocks[approved.kind]:
        code, block = _NOT_APPLIED[approved.kind]
        if approved.kind == OrderRequestKind.COMPLIMENTARY:
            message = "This order is approved as complimentary: send it as complimentary (net 0)."
        else:
            field, value = expected_discount(approved, values["subtotal"])
            message = f"This order has an approved discount: send it as {block} ({field} {value})."
        raise OrderRefused(code, message, data={
            "request_sync_id": str(approved.pk), "kind": approved.kind,
            "discount_type": approved.discount_type or None, "discount_value": _value(approved),
        })
    kind = next((k for k, block in blocks.items() if block), None)
    if kind is None:
        return None
    block = blocks[kind]
    unknown, not_approved = _BLOCK_REFUSALS[kind]
    req = order.requests.select_for_update().filter(pk=block["request_sync_id"], kind=kind).first()
    if req is None:
        raise OrderRefused(unknown)
    if req.status != OrderRequestStatus.APPROVED:
        raise OrderRefused(not_approved, f"That {req.get_kind_display().lower()} request is "
                                         f"{req.get_status_display().lower()}, not approved.")
    if kind == OrderRequestKind.COMPLIMENTARY:
        if values["tax_amount"] != 0:
            raise OrderRefused("amount_mismatch", "A complimentary bill is net 0: tax_amount must be 0.")
        return req
    field, value = expected_discount(req, values["subtotal"])
    if block.get(field) != value:
        raise OrderRefused(
            "discount_mismatch", f"The approved discount is {req.get_discount_type_display().lower()} "
            f"{_value(req)}: {field} must be {value}.",
            data={"discount_type": req.discount_type, "discount_value": _value(req), field: str(value)},
        )
    return req


def close_at_settle(order, applied, amount, at, user):
    """The settle went through: the applied approval records the amount, and a
    request still waiting is closed."""
    if applied is not None:
        applied.applied_amount, applied.applied_at, applied.modified_by = amount, at, user
        applied.save(update_fields=["applied_amount", "applied_at", "modified_by", "modified_on"])
    waiting = order.requests.filter(status=OrderRequestStatus.PENDING)
    closing = list(waiting.select_related("branch"))
    waiting.update(status=OrderRequestStatus.CLOSED, closed_at=at, modified_on=timezone.now())
    notifications.notify_all(closing, Action.CLOSED)

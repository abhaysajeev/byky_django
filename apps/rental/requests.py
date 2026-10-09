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
from apps.rental.models import (
    REQUEST_LIVE,
    DecisionChannel,
    DiscountType,
    Order,
    OrderAction,
    OrderItemStatus,
    OrderRequest,
    OrderRequestKind,
    OrderRequestStatus,
    OrderStatus,
)
from apps.rental.services import OrderRefused, _open_order, lock_order, run_once
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

# (pending, approved) refusal per kind; and the refusal another bill kind gets.
_LIVE_REFUSALS = {
    OrderRequestKind.DISCOUNT: ("discount_request_pending", "discount_approved"),
    OrderRequestKind.COMPLIMENTARY: ("complimentary_request_pending", "complimentary_approved"),
    OrderRequestKind.REPRINT: ("reprint_request_pending", "reprint_approved"),
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
        else:
            order = _open_order(session, values["order_id"])
        _refuse_if_held(order, kind)
        req = OrderRequest.objects.create(
            id=values["sync_id"], kind=kind, company=company, branch=order.branch,
            order=order, status=OrderRequestStatus.PENDING, device=session.device, requested_by=session.user,
            requested_at=values["requested_at"], reason=(values.get("reason") or "").strip(),
            created_by=session.user, modified_by=session.user,
        )
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
        if req.kind != OrderRequestKind.REPRINT:
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


def station_list(branch, *, pending_only=False, kind=None, from_date=None, to_date=None, page=1):
    """(rows, page) of one station's requests, newest first -- the operator
    app's Requests screen. Each row is the tablet's view plus the customer."""
    zone = zone_for(branch.company)
    rows = _requested_between(
        OrderRequest.objects.filter(branch=branch).select_related("order", "decided_by", "revoked_by")
        .order_by("-requested_at"), zone, from_date, to_date)
    if pending_only:
        rows = rows.filter(status=OrderRequestStatus.PENDING)
    if kind:
        rows = rows.filter(kind=kind)
    page = Paginator(rows, PAGE_SIZE).get_page(page)
    return [{**request_json(req, zone), "customer_name": req.order.customer_name}
            for req in page.object_list], page


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
    """A bill request needs the order still running; a reprint a settled one."""
    if req.kind == OrderRequestKind.REPRINT:
        return order.status == OrderStatus.COMPLETED
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
        req.status = OrderRequestStatus.APPROVED
        req.decided_by, req.decided_channel, req.decided_at = user, channel, timezone.now()
        req.decision_note, req.modified_by = (note or "").strip(), user
        req.save()
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
    return req


def revoke(user, request_id, company_ids, channel, note=""):
    """Take a discount or complimentary approval back while the order still
    runs. The tablet sees `revoked` in status, and the settle then refuses it."""
    with transaction.atomic():
        order, req = _locked(request_id, company_ids)
        if req.kind not in REVOCABLE:
            raise RequestRefused("request_not_revocable", "A reprint is approved or rejected, not revoked.")
        if req.status != OrderRequestStatus.APPROVED:
            raise RequestRefused("request_not_approved", "Only an approved request can be revoked.")
        if order.status != OrderStatus.ACTIVE:
            raise RequestRefused("order_closed", "This order is already completed or cancelled.")
        req.status = OrderRequestStatus.REVOKED
        req.revoked_by, req.revoked_channel, req.revoked_at = user, channel, timezone.now()
        req.revoke_note, req.modified_by = (note or "").strip(), user
        req.save()
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


def manager_json(req, zone, now):
    """One request as the manager app sees it: the tablet's view plus the
    order it is about -- and, once settled, its bill."""
    order = req.order
    return {
        **request_json(req, zone),
        "branch_id": req.branch_id,
        "station": req.branch.name,
        "requested_by": _name(req.requested_by),
        "order": {
            "order_no": order.order_no, "status": order.status,
            "customer_name": order.customer_name, "customer_mobile": order.customer_mobile,
            "booked_at": _local(order.booked_at, zone),
            "advance_paid": str(order.paid_amount),
            "net_amount": _money(order.net_amount),
            "vehicles": _lines(order, zone, now),
        },
    }


def _manager_rows():
    return (OrderRequest.objects
            .select_related("order", "branch", "company", "requested_by", "decided_by", "revoked_by")
            .prefetch_related("order__items__vehicle__vehicle_type"))


def manager_row(request_id):
    """One request, loaded for manager_json."""
    return _manager_rows().get(pk=request_id)


def manager_list(company_ids, *, pending_only=False, kind=None, state_id=None, branch_id=None, from_date=None,
                 to_date=None, zone=None, page=1):
    """(rows, page) of requests across the companies, newest first."""
    rows = _manager_rows().filter(company_id__in=company_ids).order_by("-requested_at")
    if zone is not None:
        rows = _requested_between(rows, zone, from_date, to_date)
    if pending_only:
        rows = rows.filter(status=OrderRequestStatus.PENDING)
    if kind:
        rows = rows.filter(kind=kind)
    if state_id:
        rows = rows.filter(branch__location__state_id=state_id)
    if branch_id:
        rows = rows.filter(branch_id=branch_id)
    page = Paginator(rows, PAGE_SIZE).get_page(page)
    now, zones = timezone.now(), {}
    out = []
    for req in page.object_list:
        zone = zones.setdefault(req.company_id, zone_for(req.company))
        out.append(manager_json(req, zone, now))
    return out, page


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
    order.requests.filter(status=OrderRequestStatus.PENDING).update(
        status=OrderRequestStatus.CLOSED, closed_at=at, modified_on=timezone.now(),
    )

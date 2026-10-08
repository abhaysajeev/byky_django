"""Requests on running orders: an operator asks, a manager decides -- on the web
or in the manager app.

Ported from legacy DMSRequests (Service_Save_RequestApproval, Approve_Request,
Reject_Request, Service_Get_Request_Approval_By_OrderNo,
Service_Save_SubmitExit_Order). Kept: the tablet asks against an order, the
approval for a discount is a rule (DiscountType + value) the tablet applies at
settle, a request still waiting at settle is closed. Not kept: the
multi-device IsIgnored patch -- one pending or approved request per order and
kind is a database rule instead; and legacy's web-only approval -- the
manager app decides too, and where each decision was made is recorded.

Only `discount` so far. Rules agreed with the owner (8 Oct 2026): only a
running order; any tablet at its station may ask or withdraw; rejected,
withdrawn and revoked requests leave room for a new one; a manager discount
and a card discount never share an order; an approval is revoked only while
the order runs; the settle must apply an approved discount exactly -- the
same %, or the AED amount (capped at the subtotal, so net may reach 0).
"""

from decimal import Decimal, InvalidOperation

from django.core.paginator import Paginator
from django.db import transaction
from django.utils import timezone

from apps.discount.models import CardDiscountClaim, ClaimStatus
from apps.rental.models import (
    REQUEST_LIVE,
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

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
PAGE_SIZE = 50

# Card-discount claims that hold an order against a manager discount.
CARD_CLAIM_LIVE = (ClaimStatus.PENDING, ClaimStatus.APPROVED)


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
    }


# -- Tablet --------------------------------------------------------------------------


def request_discount(session, values, request_data):
    """A tablet asks for a discount on a running order at its station. Returns
    (reply, done_now). The call's sync_id becomes the request's id."""
    company = session.branch.company
    zone = zone_for(company)

    def apply():
        order = _open_order(session, values["order_id"])
        live = order.requests.filter(kind=OrderRequestKind.DISCOUNT, status__in=REQUEST_LIVE).first()
        if live is not None:
            raise OrderRefused("discount_request_pending" if live.status == OrderRequestStatus.PENDING
                               else "discount_approved")
        if CardDiscountClaim.objects.filter(order=order, status__in=CARD_CLAIM_LIVE).exists():
            raise OrderRefused("card_discount_requested")
        req = OrderRequest.objects.create(
            id=values["sync_id"], kind=OrderRequestKind.DISCOUNT, company=company, branch=order.branch,
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


def withdraw(session, values, request_data):
    """A tablet takes back a request still waiting. Decided or closed ones
    stay as they are (request_closed); withdrawing twice answers the same."""
    company = session.branch.company
    zone = zone_for(company)

    def apply():
        found = OrderRequest.objects.filter(pk=values["request_sync_id"], branch=session.branch).first()
        if found is None:
            raise OrderRefused("unknown_request")
        order = lock_order(found.order_id, session.branch)
        req = OrderRequest.objects.select_for_update().get(pk=found.pk)
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


def requests_for_orders(company, order_ids):
    """Every request on these orders of the company, newest first."""
    return list(
        OrderRequest.objects.filter(company=company, order_id__in=order_ids)
        .select_related("order", "decided_by", "revoked_by").order_by("-requested_at")
    )


# -- Manager: web and manager app --------------------------------------------------


def parse_rule(discount_type, value):
    """(type, Decimal value) of an approval, or RequestRefused: a percentage
    above 0 and at most 100 with 2 decimals; an AED amount above 0 with 3."""
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


def approve(user, request_id, company_ids, channel, discount_type, value, note=""):
    rule = parse_rule(discount_type, value)
    with transaction.atomic():
        order, req = _locked(request_id, company_ids)
        if req.status != OrderRequestStatus.PENDING:
            raise _already(req)
        if order.status != OrderStatus.ACTIVE:
            raise RequestRefused("order_closed", "This order is already completed or cancelled.")
        req.status = OrderRequestStatus.APPROVED
        req.discount_type, req.discount_value = rule
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
    """Take an approval back while the order still runs. The tablet sees
    `revoked` in status, and the settle then refuses that discount."""
    with transaction.atomic():
        order, req = _locked(request_id, company_ids)
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


def manager_json(req, zone, now):
    """One request as the manager app sees it: the tablet's view plus the
    order it is about."""
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


def manager_list(company_ids, *, pending_only=False, branch_id=None, page=1):
    """(rows, page) of requests across the companies, newest first."""
    rows = _manager_rows().filter(company_id__in=company_ids).order_by("-requested_at")
    if pending_only:
        rows = rows.filter(status=OrderRequestStatus.PENDING)
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
    if req.discount_type == DiscountType.PERCENT:
        return "discount_percentage", req.discount_value.quantize(Decimal("0.01"))
    return "discount_amount", min(req.discount_value, subtotal)


def check_at_settle(order, manager_discount, subtotal):
    """The approved manager discount the settle applies, or None. Refused when
    the block names a request that is not this order's approved one, carries
    other figures than the approval, or is missing while an approval waits."""
    approved = (order.requests.select_for_update()
                .filter(kind=OrderRequestKind.DISCOUNT, status=OrderRequestStatus.APPROVED).first())
    if not manager_discount:
        if approved is not None:
            field, value = expected_discount(approved, subtotal)
            raise OrderRefused(
                "approved_discount_not_applied",
                f"This order has an approved discount: send it as manager_discount ({field} {value}).",
                data={"request_sync_id": str(approved.pk), "discount_type": approved.discount_type,
                      "discount_value": _value(approved)},
            )
        return None
    req = (order.requests.select_for_update()
           .filter(pk=manager_discount["request_sync_id"], kind=OrderRequestKind.DISCOUNT).first())
    if req is None:
        raise OrderRefused("unknown_discount_request")
    if req.status != OrderRequestStatus.APPROVED:
        raise OrderRefused("discount_request_not_approved",
                           f"That discount request is {req.get_status_display().lower()}, not approved.")
    field, value = expected_discount(req, subtotal)
    if manager_discount.get(field) != value:
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

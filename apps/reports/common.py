"""Pieces several reports share: the request context, the company-scoped
starting querysets, the filters every order report applies the same way, the
payment-mode columns and how a person is named."""

from collections import defaultdict
from dataclasses import dataclass

from django.db.models import Q, Sum

from apps.rental.models import CreditNote, CreditNoteStatus, OrderItemStatus, Payment, PaymentKind
from apps.reports.base import MONEY, ZERO, Column

# The lines that bill: out now, or back. Replaced, removed and cancelled lines do not.
BILLING = (OrderItemStatus.ACTIVE, OrderItemStatus.RETURNED)


@dataclass
class Ctx:
    user: object
    zone: object
    f: object            # apps.reports.filters.Filters
    now: object


# -- Filters every order-shaped report applies the same way ---------------------------


def place(qs, f, branch="branch"):
    """State and station, on whichever branch the rows belong to."""
    if f.get("state"):
        qs = qs.filter(**{f"{branch}__location__state_id": f.get("state")})
    if f.get("branch"):
        qs = qs.filter(**{f"{branch}_id": f.get("branch")})
    return qs


def cashier(qs, f, order="order__"):
    """The cashier: the user who opened (booked) or closed (settled) the order."""
    if not f.get("cashier"):
        return qs
    return qs.filter(Q(**{f"{order}created_by_id": f.get("cashier")})
                     | Q(**{f"{order}invoice__issued_by_id": f.get("cashier")}))


def customer(qs, f, order="order__"):
    if f.get("customer"):
        qs = qs.filter(**{f"{order}customer_name__icontains": f.get("customer")})
    if f.get("phone"):
        digits = "".join(ch for ch in f.get("phone") if ch.isdigit())
        qs = qs.filter(**{f"{order}customer_mobile__icontains": digits}) if digits else qs.none()
    if f.get("order_no"):
        qs = qs.filter(**{f"{order}order_no__icontains": f.get("order_no")})
    return qs


def vehicle_q(f, prefix=""):
    """The line filter: category, vehicle type, vehicle number -- on one line."""
    q = Q()
    if f.get("category"):
        q &= Q(**{f"{prefix}vehicle__vehicle_type__category_id": f.get("category")})
    if f.get("vehicle_type"):
        q &= Q(**{f"{prefix}vehicle__vehicle_type_id": f.get("vehicle_type")})
    if f.get("vehicle_no"):
        text = f.get("vehicle_no")
        q &= (Q(**{f"{prefix}vehicle__vehicle_code__icontains": text})
              | Q(**{f"{prefix}vehicle__vehicle_name__icontains": text})
              | Q(**{f"{prefix}vehicle__identifier__icontains": text}))
    return q


# -- Payments by mode ----------------------------------------------------------------

PAY_KEYS = ("cash", "card", "tap", "upi", "other")


def bucket(mode_name):
    """A payment mode is a name per company: Cash, Card, Tap & Pay and UPI are
    told apart by their names; anything else is Other."""
    name = (mode_name or "").lower()
    for key in ("tap", "upi", "card", "cash"):
        if key in name:
            return key
    return "other"


def payments_by_order(order_ids):
    """{order_id: {bucket: amount}} -- advance and settlement, less refunds."""
    out = defaultdict(lambda: dict.fromkeys(PAY_KEYS, ZERO))
    rows = (Payment.objects.filter(order_id__in=list(order_ids))
            .values("order_id", "mode__name", "kind").annotate(total=Sum("amount")).order_by())
    for row in rows:
        sign = -1 if row["kind"] == PaymentKind.REFUND else 1
        out[row["order_id"]][bucket(row["mode__name"])] += sign * (row["total"] or ZERO)
    return out


def references_by_order(order_ids):
    """{order_id: "ref1, ref2"} -- the payments' reference numbers."""
    out = defaultdict(list)
    for order_id, ref in (Payment.objects.filter(order_id__in=list(order_ids)).exclude(reference_no="")
                          .order_by("paid_at").values_list("order_id", "reference_no")):
        if ref not in out[order_id]:
            out[order_id].append(ref)
    return {k: ", ".join(v) for k, v in out.items()}


def pay_columns(labels, with_other, part="row"):
    """The payment columns: `labels` maps cash / card / tap / upi to the
    report's own column names; Other only when some payment is in it."""
    cols = [Column(f"pay_{key}", labels[key], MONEY, part=part) for key in ("cash", "card", "tap", "upi")]
    if with_other:
        cols.append(Column("pay_other", labels.get("other", "Paid By Other (AED)"), MONEY, part=part))
    return cols


def pay_cells(paid):
    return {f"pay_{key}": paid.get(key, ZERO) for key in PAY_KEYS}


def credit_notes_by_order(order_ids):
    """{order_id: approved credit notes' total}."""
    return dict(CreditNote.objects.filter(order_id__in=list(order_ids), status=CreditNoteStatus.APPROVED)
                .values("order_id").annotate(total=Sum("net_amount")).order_by().values_list("order_id", "total"))


# -- People -------------------------------------------------------------------------


def person(user):
    """'Name ( code )' -- the employee code when the user is staff."""
    if user is None:
        return None
    name = user.display_name or user.username
    code = getattr(getattr(user, "employee", None), "employee_code", "") if user.employee_id else ""
    return f"{name} ( {code} )" if code else name

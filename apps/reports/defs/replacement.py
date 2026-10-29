"""Replacement Details -- the client's sheet "Replacement Details Report".

One row per vehicle replaced in the range (the tablet's replace call, by its
time): the order, who replaced it and when, the vehicle taken off and the one
put on. A vehicle filter matches either of the two. Cashier: the user who
replaced it, opened the order or closed it.
"""

from django.db.models import Prefetch, Q

from apps.rental.models import OrderAction, OrderEvent, OrderItem
from apps.rental.scoping import orders_for
from apps.reports import common
from apps.reports.base import COUNT, DATETIME, Column, Kpi, Report, Row, local, paged


class ReplacementDetails(Report):
    slug = "replacement-details"
    title = "Replacement Details"
    sheet_title = "Replacement Details Report"
    page_code = "reports.replacement_details"
    filters = ("state", "branch", "cashier", "category", "vehicle_type", "customer", "phone", "order_no",
               "vehicle_no")
    unit = ("replacement", "replacements")
    empty = "No vehicle was replaced in this range."
    description = "Every vehicle swapped on a running order: when, by whom, and which for which."

    def build(self, ctx, page=None, per_page=None):
        f = ctx.f
        events = OrderEvent.objects.filter(order__in=common.place(
            common.customer(orders_for(ctx.user), f, order=""), f).values("pk"),
            action=OrderAction.REPLACE, happened_at__gte=f.since, happened_at__lt=f.until)
        if f.get("cashier"):
            events = events.filter(Q(user_id=f.get("cashier")) | Q(order__created_by_id=f.get("cashier"))
                                   | Q(order__invoice__issued_by_id=f.get("cashier")))
        if f.any_vehicle:
            events = events.filter(common.vehicle_q(f, "item__") | common.vehicle_q(f, "new_item__"))
        events = (events.select_related("order__branch", "user__employee", "item__vehicle", "new_item__vehicle")
                  .prefetch_related(Prefetch("order__items", queryset=OrderItem.objects.only("order_id", "status")))
                  .order_by("happened_at", "pk"))

        columns = [
            Column("branch", "Branch", width=22),
            Column("order_no", "Order Number", width=20),
            Column("booked", "Order Date And Time", DATETIME, width=21),
            Column("status", "Order Status", width=17),
            Column("customer", "Customer Name", width=20),
            Column("phone", "Phone No", width=16),
            Column("replaced_at", "Replaced Date And Time", DATETIME, width=21),
            Column("replaced_by", "Replaced By", width=26),
            Column("old", "Old Vehicle Number", width=16),
            Column("new", "Replaced Vehicle Number", width=16),
            Column("vehicle_status", "Vehicle Status", width=14),
        ]
        rows = [Row({
            "branch": e.order.branch.name, "order_no": e.order.order_no, "booked": local(e.order.booked_at, ctx.zone),
            "status": common.order_status(e.order), "customer": e.order.customer_name,
            "phone": e.order.customer_mobile, "replaced_at": local(e.happened_at, ctx.zone),
            "replaced_by": common.person(e.user),
            "old": e.item.vehicle.vehicle_code if e.item else None,
            "new": e.new_item.vehicle.vehicle_code if e.new_item else None,
            "vehicle_status": "Replaced",
        }) for e in events]
        orders = len({r.cells["order_no"] for r in rows})
        return paged(rows, page, per_page, columns=columns, total=None, count=len(rows),
                     kpis=[Kpi("Replacements", len(rows), COUNT), Kpi("Orders", orders, COUNT),
                           Kpi("Branches", len({r.cells["branch"] for r in rows}), COUNT)])

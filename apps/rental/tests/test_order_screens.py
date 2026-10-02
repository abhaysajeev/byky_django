"""The Orders screen: the list (columns, filters, KPIs) and the read-only
detail page."""

import datetime
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.company.models import PaymentMode
from apps.devices.models import Device, DeviceStatus
from apps.fleet.models import UOM, Brand, Category, Vehicle, VehicleType
from apps.rental.models import (
    Invoice,
    Order,
    OrderAction,
    OrderEvent,
    OrderItem,
    OrderItemStatus,
    OrderStatus,
    Payment,
    PaymentKind,
)
from apps.rental.tests.conftest import make_customer
from apps.rental.tests.test_views import revoke
from core.enums import Channel
from core.ids import uuid7
from core.models import User
from core.timezones import zone_for

LIST = "/rental/order/list/"


@pytest.fixture
def shop(world):
    """A tablet, a customer, two bikes and cash at Corniche 1; a tablet at the
    other company's station."""
    company = world["company"]
    category = Category.objects.create(company=company, category_code="C", category_name="Bikes")
    brand = Brand.objects.create(company=company, brand_code="B", brand_name="Byky")
    monaco = VehicleType.objects.create(company=company, category=category, brand=brand, vehicle_type_name="Monaco")
    uom = UOM.objects.create(company=company, uom_code="N", uom_name="Number")

    def device(co, installation_id):
        return Device.objects.create(company=co, installation_id=installation_id, platform="android",
                                     channel=Channel.OPERATOR, status=DeviceStatus.APPROVED, approved_at=timezone.now())

    def bike(name):
        return Vehicle.objects.create(company=company, vehicle_code=name, vehicle_name=name, vehicle_type=monaco,
                                      uom=uom, current_branch=world["adc1"])

    return {
        "tablet": device(company, "till-1"), "their_tablet": device(world["other"], "till-9"),
        "ahmed": make_customer(world, first_name="Ahmed", last_name="Al Mansoori"),
        "mo41": bike("MO 41"), "dc02": bike("DC 02"),
        "cash": PaymentMode.objects.create(company=company, name="Cash"),
    }


def at(day_offset=0, hour=16, minute=0, world=None):
    zone = zone_for(world["company"]) if world else datetime.UTC
    today = timezone.now().astimezone(zone).date()
    return datetime.datetime.combine(today + datetime.timedelta(days=day_offset), datetime.time(hour, minute),
                                     tzinfo=zone)


def make_order(world, shop, order_no, *, status=OrderStatus.ACTIVE, lines=("active",), day=0, **fields):
    start = at(day, world=world)
    order = Order.objects.create(
        id=uuid7(), company=world["company"], branch=world["adc1"], device=shop["tablet"], customer=shop["ahmed"],
        customer_name="Ahmed Al Mansoori", customer_mobile="971501234567", order_no=order_no, status=status,
        booked_at=start, start_time=start, **fields,
    )
    for line_status, vehicle in zip(lines, (shop["mo41"], shop["dc02"]), strict=False):
        done = line_status != "active"
        OrderItem.objects.create(
            id=uuid7(), order=order, vehicle=vehicle, status=line_status, package_minutes=60, start_time=start,
            expected_end_time=start + datetime.timedelta(hours=1), base_fare=50,
            end_time=start + datetime.timedelta(minutes=72) if done else None,
            run_minutes=72 if line_status == "returned" else None,
            overtime_amount=10 if line_status == "returned" else None,
            total_amount=60 if line_status == "returned" else None,
            reason="Chain broken" if line_status == "replaced" else "",
        )
    return order


def settle(order, net="63.00", day=0, world=None):
    Order.objects.filter(pk=order.pk).update(
        status=OrderStatus.COMPLETED, completed_at=at(day, 17, 15, world=world), subtotal=60,
        discount_amount=0, tax_percentage=5, tax_amount=3, rounding_adjustment=0, net_amount=Decimal(net),
        amount_received=Decimal(net),
    )


def rows(client_in, **params):
    return client_in.get(LIST, params).context["rows"]


def test_the_list_shows_this_companys_orders_with_their_columns(client_in, world, shop):
    make_order(world, shop, "ADC0000231", is_direct_bill=True)
    Order.objects.create(
        id=uuid7(), company=world["other"], branch=world["theirs"], device=shop["their_tablet"],
        customer=make_customer({"company": world["other"]}, customer_code="CU9", mobile_no="509999999"),
        order_no="THEIRS-1", booked_at=at(world=world), start_time=at(world=world),
    )

    response = client_in.get(LIST)

    html = response.content.decode()
    assert "ADC0000231" in html and "THEIRS-1" not in html
    [row] = response.context["rows"]
    assert (row["customer"], row["mobile"], row["station"], row["tablet"]) == (
        "Ahmed Al Mansoori", "971501234567", "Abu Dhabi Corniche 1", shop["tablet"].device_registration_id)
    assert (row["status_label"], row["is_direct"]) == ("Active", True)
    assert "Direct rental" in html


def test_the_status_says_what_is_left_to_do(client_in, world, shop):
    make_order(world, shop, "O-OUT")
    make_order(world, shop, "O-BACK", lines=("returned",))
    settle(make_order(world, shop, "O-DONE", lines=("returned",)), world=world)
    make_order(world, shop, "O-CANCEL", status=OrderStatus.CANCELLED, lines=("cancelled",))

    labels = {r["order_no"]: r["status_label"] for r in rows(client_in)}

    assert labels == {"O-OUT": "Active", "O-BACK": "Awaiting settlement", "O-DONE": "Completed",
                      "O-CANCEL": "Cancelled"}
    for status, expected in (("active", "O-OUT"), ("awaiting", "O-BACK"), ("completed", "O-DONE"),
                             ("cancelled", "O-CANCEL")):
        assert [r["order_no"] for r in rows(client_in, status=status)] == [expected]


def test_search_finds_an_order_by_number_or_phone(client_in, world, shop):
    make_order(world, shop, "ADC0000231")
    other = make_order(world, shop, "ADC0000232", lines=())
    Order.objects.filter(pk=other.pk).update(customer_name="Fatima Hassan", customer_mobile="971509999999")

    assert [r["order_no"] for r in rows(client_in, q="0231")] == ["ADC0000231"]
    assert [r["order_no"] for r in rows(client_in, q="509999")] == ["ADC0000232"]
    assert [r["order_no"] for r in rows(client_in, q="fatima")] == ["ADC0000232"]


def test_the_dates_and_the_station_narrow_the_list(client_in, world, shop):
    make_order(world, shop, "TODAY")
    make_order(world, shop, "LAST-WEEK", day=-7, lines=())
    today = at(world=world).date().isoformat()

    assert [r["order_no"] for r in rows(client_in, **{"from": today, "to": today})] == ["TODAY"]
    assert [r["order_no"] for r in rows(client_in, branch=world["adc1"].pk)] == ["TODAY", "LAST-WEEK"]


def test_the_newest_booking_comes_first_and_the_list_pages(client_in, world, shop):
    for n in range(52):
        order = make_order(world, shop, f"O-{n:03d}", lines=())
        Order.objects.filter(pk=order.pk).update(booked_at=at(world=world) + datetime.timedelta(minutes=n))

    first = client_in.get(LIST).context
    assert (first["page"].paginator.count, len(first["rows"]), first["rows"][0]["order_no"]) == (52, 50, "O-051")
    assert [r["order_no"] for r in rows(client_in, page=2)] == ["O-001", "O-000"]


def test_the_kpis_are_now_and_today(client_in, world, shop):
    make_order(world, shop, "OUT-2", lines=("active", "active"))
    make_order(world, shop, "BACK", lines=())
    settle(make_order(world, shop, "TODAY-1", lines=()), net="63.00", world=world)
    settle(make_order(world, shop, "TODAY-2", lines=()), net="40.00", world=world)
    settle(make_order(world, shop, "YESTERDAY", day=-1, lines=()), net="99.00", day=-1, world=world)

    kpis = {k["label"]: k["value"] for k in client_in.get(LIST).context["kpis"]}

    assert kpis == {"Active orders now": 2, "Vehicles out now": 2, "Settled today": 2,
                    "Settled revenue today": "AED 103.00"}


def test_the_list_needs_read(client_in):
    revoke(client_in, "read", page="rental.order")

    assert client_in.get(LIST).status_code == 403


# -- Detail ------------------------------------------------------------------------------


def test_a_settled_order_shows_its_bill_vehicles_payments_invoice_and_history(client_in, world, shop):
    """MO 41 replaced by DC 02 ("Chain broken"), DC 02 returned late, the bill
    settled at AED 63.00 and paid in cash."""
    order = make_order(world, shop, "ADC0000231", lines=("replaced", "returned"))
    returned = order.items.get(status=OrderItemStatus.RETURNED)
    OrderItem.objects.filter(pk=returned.pk).update(replaced_item=order.items.get(status=OrderItemStatus.REPLACED))
    settle(order, world=world)
    Payment.objects.create(id=uuid7(), order=order, kind=PaymentKind.ADVANCE, mode=shop["cash"], amount=63,
                           paid_at=at(world=world), reference_no="R-1")
    user = User.objects.get(username="sara.k")
    Invoice.objects.create(
        id=uuid7(), order=order, company=world["company"], branch=world["adc1"], device=shop["tablet"],
        issued_by=user, invoice_no="ADC0000231", issued_at=at(0, 17, 15, world=world), company_name="BYKY",
        branch_name="Abu Dhabi Corniche 1", subtotal=60, discount_amount=0, tax_percentage=5, tax_amount=3,
        rounding_adjustment=0, net_amount=63,
    )
    OrderEvent.objects.create(id=uuid7(), order=order, action=OrderAction.RETURN, item=returned,
                              happened_at=at(0, 17, 12, world=world), user=user, device=shop["tablet"],
                              detail={"run_minutes": 72, "total_amount": "60.00"}, request_hash="x")

    response = client_in.get(f"/rental/order/{order.pk}/")

    assert response.status_code == 200
    html = response.content.decode()
    for text in ("ADC0000231", "Completed", "AED 63.00", "Chain broken", "Replaced", "Returned", "R-1",
                 "DC 02 · 72 min · AED 60.00"):
        assert text in html, text
    assert "Not billed yet" not in html
    assert response.context["invoice"]["invoice_no"] == "ADC0000231"
    assert [i["replaced_by"] for i in response.context["items"] if i["status"] == "replaced"] == ["DC 02"]


def test_an_order_on_rent_is_not_billed_yet(client_in, world, shop):
    order = make_order(world, shop, "ADC0000231")

    html = client_in.get(f"/rental/order/{order.pk}/").content.decode()

    assert "Not billed yet" in html and "Active" in html


def test_another_companys_order_is_not_found(client_in, world, shop):
    theirs = Order.objects.create(
        id=uuid7(), company=world["other"], branch=world["theirs"], device=shop["their_tablet"],
        customer=make_customer({"company": world["other"]}, customer_code="CU9", mobile_no="509999999"),
        order_no="THEIRS-1", booked_at=at(world=world), start_time=at(world=world),
    )

    assert client_in.get(f"/rental/order/{theirs.pk}/").status_code == 404

"""POST /api/v1/operator/orders and /orders/detail through HTTP, as the till
app sees them -- the booking in order_lifecycle_design.md 3.1, resent,
conflicting and refused, and read back from another tablet."""

import copy
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.company.models import PaymentMode
from apps.crew.models import Designation, Employee
from apps.devices import services as devices_services
from apps.devices.models import BillKind, Device, DeviceMapping, DeviceSettings, DeviceStatus
from apps.fare.tests import test_api as fare_api
from apps.fare.tests.conftest import make_fare
from apps.fleet.models import UOM, Vehicle
from apps.portal.models import Role
from apps.rental import api
from apps.rental.models import (
    Order,
    OrderAction,
    OrderEvent,
    OrderItem,
    OrderItemStatus,
    Payment,
    PaymentStatus,
)
from apps.rental.tests.conftest import make_customer
from core.enums import ApprovalStatus, Channel
from core.ids import uuid7
from core.models import User

call, shape = fare_api.call, fare_api.shape
world, token, fresh_throttle = fare_api.world, fare_api.token, fare_api.fresh_throttle

BOOK = "/api/v1/operator/orders"
DETAIL = "/api/v1/operator/orders/detail"
APPROVED = ApprovalStatus.APPROVED


@pytest.fixture
def shop(world):
    """Two bikes and a kart at Corniche 1, one at Corniche 2, Ahmed, and two
    ways to pay."""
    company = world["company"]
    uom = UOM.objects.create(company=company, uom_code="NO", uom_name="Number", approval_status=APPROVED)

    def vehicle(code, branch):
        return Vehicle.objects.create(company=company, vehicle_code=code, vehicle_name=code,
                                      vehicle_type=world["monaco"], uom=uom, current_branch=branch,
                                      approval_status=APPROVED)

    return {
        "mo41": vehicle("MO 41", world["adc1"]), "dc02": vehicle("DC 02", world["adc1"]),
        "mo42": vehicle("MO 42", world["adc1"]), "away": vehicle("MO 99", world["adc2"]),
        "ahmed": make_customer(world, first_name="Ahmed", last_name="Al Mansoori"),
        "cash": PaymentMode.objects.create(company=company, name="Cash"),
        "card": PaymentMode.objects.create(company=company, name="Card"),
        "tablet": Device.objects.get(installation_id="till-1"),
        "fare": make_fare(world, package_minutes=60, approval_status=APPROVED),
    }


def receipt(device, branch, number):
    """The receipt number `device` prints for `number` at `branch`."""
    counter = devices_services.counter_for(device, branch, BillKind.ORDER)
    return devices_services.format_bill_number(counter, number)


def booking(world, shop, number=231, **overrides):
    """The design's 3.1 booking: MO 41 and DC 02 for an hour, AED 100 down --
    60 cash, 40 by card."""
    data = {
        "sync_id": str(uuid7()), "order_no": receipt(shop["tablet"], world["adc1"], number),
        "customer_id": shop["ahmed"].pk,
        "booked_at": "2026-10-02 16:00:05", "start_time": "2026-10-02 16:00:00",
        "items": [
            {"sync_id": str(uuid7()), "vehicle_id": shop[code].pk, "fare_id": shop["fare"].pk,
             "package_minutes": 60,
             "start_time": "2026-10-02 16:00:00", "expected_end_time": "2026-10-02 17:00:00",
             "rate": "50.00", "amount": "50.00", "tax_amount": "2.50", "total_amount": "50.00"}
            for code in ("mo41", "dc02")
        ],
        "total_amount": "100.00", "tax_percentage": "5.00", "total_tax": "5.00", "net_amount": "105.00",
        "payments": [
            {"sync_id": str(uuid7()), "kind": "advance", "payment_mode_id": shop["cash"].pk,
             "amount": "60.00", "paid_at": "2026-10-02 16:00:05"},
            {"sync_id": str(uuid7()), "kind": "advance", "payment_mode_id": shop["card"].pk,
             "amount": "40.00", "reference_no": "448812", "reference_date": "2026-10-02",
             "paid_at": "2026-10-02 16:00:05"},
        ],
    }
    data.update(overrides)
    return data


def counter(world, shop):
    return devices_services.counter_for(shop["tablet"], world["adc1"], BillKind.ORDER).last_number


# -- Booking -----------------------------------------------------------------------


def test_a_booking_saves_the_order_its_vehicles_and_payments(client, world, token, shop):
    request_data = booking(world, shop)

    body = call(client, BOOK, request_data, token=token).json()

    assert body["code"] == "ok", body
    data = body["data"]
    assert (data["sync_id"], data["order_no"], data["status"]) == (
        request_data["sync_id"], request_data["order_no"], "active")
    assert (data["net_amount"], data["amount_received"], data["paid_amount"], data["balance_due"]) == (
        "105.00", "100.00", "100.00", "5.00")
    assert data["payment_status"] == PaymentStatus.PARTLY_PAID and data["items_out"] == 2
    assert data["customer"] == {"id": shop["ahmed"].pk, "name": "Ahmed Al Mansoori", "mobile": "971501234567"}
    assert data["booked_at"] == "2026-10-02 16:00:05"
    assert [item["sync_id"] for item in data["items"]] == [item["sync_id"] for item in request_data["items"]]
    assert {item["vehicle"]["name"] for item in data["items"]} == {"MO 41", "DC 02"}
    assert [(p["payment_mode"]["name"], p["amount"], p["reference_no"]) for p in data["payments"]] == [
        ("Cash", "60.00", ""), ("Card", "40.00", "448812")]
    # The tablet's ids are the rows' ids.
    assert set(map(str, OrderItem.objects.values_list("id", flat=True))) == {
        item["sync_id"] for item in request_data["items"]}
    assert set(map(str, Payment.objects.values_list("id", flat=True))) == {
        p["sync_id"] for p in request_data["payments"]}
    # One history row, holding the reply.
    event = OrderEvent.objects.get()
    assert (str(event.pk), event.action, event.response) == (request_data["sync_id"], OrderAction.BOOK, data)
    assert event.detail == {"items": 2, "advance": "100.00"}
    # The receipt counter moved up to the number used.
    assert counter(world, shop) == 231 and data["next_order_number"] == 232
    # The Swagger example is exactly what a tablet gets.
    assert shape(data) == shape(api._ORDER_SAMPLE)


def test_a_resent_booking_is_a_duplicate_with_the_same_reply(client, world, token, shop):
    request_data = booking(world, shop)
    first = call(client, BOOK, request_data, token=token).json()

    # Same call, keys in another order.
    again = call(client, BOOK, dict(reversed(list(request_data.items()))), token=token).json()

    assert (again["code"], again["data"]) == ("duplicate", first["data"])
    assert (Order.objects.count(), OrderItem.objects.count(), Payment.objects.count()) == (1, 2, 2)
    assert OrderEvent.objects.count() == 1


def test_the_same_sync_id_with_another_body_is_a_conflict(client, world, token, shop):
    request_data = booking(world, shop)
    call(client, BOOK, request_data, token=token)

    changed = copy.deepcopy(request_data)
    changed["net_amount"] = "110.00"
    body = call(client, BOOK, changed, token=token).json()

    assert (body["code"], body["data"]) == ("sync_id_conflict", {"retry": False})
    assert Order.objects.get().net_amount == Decimal("105.00")


def test_a_booking_with_no_payment_is_unpaid(client, world, token, shop):
    body = call(client, BOOK, booking(world, shop, payments=[]), token=token).json()

    assert body["code"] == "ok"
    assert (body["data"]["payment_status"], body["data"]["payments"]) == (PaymentStatus.UNPAID, [])


def test_the_counter_only_moves_forward(client, world, token, shop):
    call(client, BOOK, booking(world, shop, number=231), token=token)
    later = booking(world, shop, number=220)
    for item, vehicle in zip(later["items"], ("mo42",), strict=False):
        item["vehicle_id"] = shop[vehicle].pk
    later["items"] = later["items"][:1]

    assert call(client, BOOK, later, token=token).json()["code"] == "ok"
    assert counter(world, shop) == 231               # a late upload of an older receipt


def test_an_order_number_not_in_this_tablets_shape_is_kept_and_counts_nothing(client, world, token, shop):
    body = call(client, BOOK, booking(world, shop, order_no="HANDWRITTEN-7"), token=token).json()

    assert (body["code"], body["data"]["order_no"]) == ("ok", "HANDWRITTEN-7")
    assert counter(world, shop) == 0


# -- Refusals ------------------------------------------------------------------------


def refusal(client, token, request_data):
    body = call(client, BOOK, request_data, token=token).json()
    return body["code"], body["message"]


def test_a_used_order_number_is_refused(client, world, token, shop):
    call(client, BOOK, booking(world, shop), token=token)
    again = booking(world, shop)                       # same number, new ids
    again["items"] = again["items"][:1]
    again["items"][0]["vehicle_id"] = shop["mo42"].pk

    assert refusal(client, token, again)[0] == "order_no_used"


def test_a_vehicle_already_out_is_refused(client, world, token, shop):
    call(client, BOOK, booking(world, shop), token=token)

    code, message = refusal(client, token, booking(world, shop, number=232))

    assert code == "vehicle_already_rented" and str(shop["mo41"].pk) in message
    assert Order.objects.count() == 1


def test_the_same_vehicle_twice_is_refused(client, world, token, shop):
    request_data = booking(world, shop)
    request_data["items"][1]["vehicle_id"] = shop["mo41"].pk

    assert refusal(client, token, request_data)[0] == "vehicle_repeated"


def test_an_item_id_already_used_is_refused(client, world, token, shop):
    first = booking(world, shop)
    call(client, BOOK, first, token=token)
    again = booking(world, shop, number=232)
    again["items"] = [{**again["items"][0], "sync_id": first["items"][0]["sync_id"],
                       "vehicle_id": shop["mo42"].pk}]

    assert refusal(client, token, again)[0] == "item_id_used"


def test_a_payment_id_already_used_is_refused(client, world, token, shop):
    first = booking(world, shop)
    call(client, BOOK, first, token=token)
    again = booking(world, shop, number=232)
    again["items"] = [{**again["items"][0], "vehicle_id": shop["mo42"].pk}]
    again["payments"][0]["sync_id"] = first["payments"][0]["sync_id"]

    assert refusal(client, token, again)[0] == "payment_id_used"


@pytest.mark.parametrize(("change", "code"), [
    (lambda data, world, shop: data.update(customer_id=999999), "unknown_customer"),
    (lambda data, world, shop: data["payments"][0].update(payment_mode_id=999999), "unknown_payment_mode"),
    (lambda data, world, shop: data["items"][0].update(vehicle_id=999999), "unknown_vehicle"),
    (lambda data, world, shop: data["items"][0].update(vehicle_id=shop["away"].pk), "vehicle_not_at_station"),
    (lambda data, world, shop: data["items"][0].update(fare_id=999999), "unknown_fare"),
    (lambda data, world, shop: data["items"][0].update(offer_id=999999), "unknown_offer"),
], ids=["customer", "payment-mode", "vehicle", "vehicle-elsewhere", "fare", "offer"])
def test_unknown_ids_are_refused_and_nothing_is_written(client, world, token, shop, change, code):
    request_data = booking(world, shop)
    change(request_data, world, shop)

    body = call(client, BOOK, request_data, token=token).json()

    assert (body["code"], body["data"]) == (code, {"retry": False})
    assert (Order.objects.count(), OrderEvent.objects.count(), counter(world, shop)) == (0, 0, 0)


@pytest.mark.parametrize(("change", "field"), [
    (lambda data: data["payments"][0].update(kind="refund"), "payments"),
    (lambda data: data["payments"][0].update(amount="0.00"), "payments"),
    (lambda data: data["items"][1].update(sync_id=data["items"][0]["sync_id"]), "items"),
    (lambda data: data["payments"][1].update(sync_id=data["payments"][0]["sync_id"]), "payments"),
    (lambda data: data["items"][0].update(expected_end_time="2026-10-02 15:00:00"), "items"),
    (lambda data: data.update(items=[]), "items"),
    (lambda data: data.pop("booked_at"), "booked_at"),
], ids=["not-advance", "zero-payment", "item-ids", "payment-ids", "end-before-start", "no-items", "no-booked-at"])
def test_the_request_is_checked(client, world, token, shop, change, field):
    request_data = booking(world, shop)
    change(request_data)

    body = call(client, BOOK, request_data, token=token).json()

    assert body["code"] == "invalid_request" and field in body["data"]["errors"], body


def test_booking_is_for_the_operator_app_only(client, world, token, shop):
    assert client.post("/api/v1/manager/orders", {}, content_type="application/json",
                       HTTP_AUTHORIZATION=f"Bearer {token}").json()["code"] == "wrong_channel"


# -- Detail --------------------------------------------------------------------------


def sign_in_tablet(client, world, *, code, installation_id, branch):
    """Another operator on another tablet, mapped to `branch`."""
    company = world["company"]
    branch.is_multi_device = True
    branch.save()
    DeviceSettings.objects.get_or_create(branch=branch, defaults={
        "settings_code": f"S-{branch.short_code}", "order_no_prefix": branch.short_code,
        "approval_status": APPROVED})
    employee = Employee.objects.create(company=company, employee_code=code, first_name="Mona",
                                       designation=Designation.objects.get(company=company))
    User.objects.create_user(code, fare_api.PASSWORD, display_name="Mona", company=company,
                             role=Role.objects.get(company=company), employee=employee,
                             allowed_channels=[Channel.OPERATOR])
    device = Device.objects.create(company=company, installation_id=installation_id, platform="android",
                                   channel=Channel.OPERATOR, status=DeviceStatus.APPROVED,
                                   approved_at=timezone.now())
    DeviceMapping.objects.create(device=device, branch=branch, from_date=timezone.now())
    response = call(client, "/api/v1/operator/auth/login",
                    {"username": code, "password": fare_api.PASSWORD, "installation_id": installation_id})
    assert response.status_code == 200, response.json()
    return response.json()["data"]["tokens"]["access"]


def test_any_tablet_at_the_station_reads_an_order_by_id_or_number(client, world, token, shop):
    request_data = booking(world, shop)
    booked = call(client, BOOK, request_data, token=token).json()["data"]
    other_tablet = sign_in_tablet(client, world, code="OPR002", installation_id="till-2", branch=world["adc1"])

    by_id = call(client, DETAIL, {"sync_id": request_data["sync_id"]}, token=other_tablet).json()
    by_number = call(client, DETAIL, {"order_no": request_data["order_no"]}, token=other_tablet).json()

    assert by_id["code"] == by_number["code"] == "ok"
    assert by_id["data"] == by_number["data"]
    # The same order -- only the counter is the asking tablet's own.
    assert {**by_id["data"], "next_order_number": None} == {**booked, "next_order_number": None}
    assert by_id["data"]["next_order_number"] == 1


def test_another_stations_tablet_cannot_read_the_order(client, world, token, shop):
    request_data = booking(world, shop)
    call(client, BOOK, request_data, token=token)
    elsewhere = sign_in_tablet(client, world, code="OPR003", installation_id="till-3", branch=world["adc2"])

    body = call(client, DETAIL, {"sync_id": request_data["sync_id"]}, token=elsewhere).json()

    assert (body["code"], body["data"]) == ("unknown_order", {"retry": False})


@pytest.mark.parametrize(("request_data", "field"), [
    ({}, "sync_id"),
    ({"sync_id": str(uuid7()), "order_no": "X1"}, "order_no"),
    ({"sync_id": "not-a-uuid"}, "sync_id"),
])
def test_the_detail_lookup_is_checked(client, world, token, request_data, field):
    body = call(client, DETAIL, request_data, token=token).json()

    assert body["code"] == "invalid_request" and field in body["data"]["errors"]


def test_an_unknown_order_is_not_found(client, world, token):
    body = call(client, DETAIL, {"order_no": "NOPE"}, token=token).json()

    assert body["code"] == "unknown_order"


def test_the_order_endpoints_are_in_the_api_docs(client, db):
    schema = client.get("/api/schema/").content.decode()

    assert "/api/v1/{app}/orders/detail" in schema and "item_id_used" in schema


def test_a_returned_item_no_longer_counts_as_out(client, world, token, shop):
    request_data = booking(world, shop)
    call(client, BOOK, request_data, token=token)
    OrderItem.objects.filter(pk=request_data["items"][0]["sync_id"]).update(status=OrderItemStatus.RETURNED)

    data = call(client, DETAIL, {"sync_id": request_data["sync_id"]}, token=token).json()["data"]

    assert data["items_out"] == 1

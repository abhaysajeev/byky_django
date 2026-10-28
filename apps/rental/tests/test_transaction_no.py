"""Transaction numbers (owner, 28 Oct 2026): the tablet counts the day's
bookings and sends `<prefix>-<n>` with each; the server stores it as sent, and
login tells the tablet its prefix and today's last number."""

from django.utils import timezone

from apps.devices import services as devices_services
from apps.devices.models import DeviceMapping
from apps.rental import live
from apps.rental.models import Order
from apps.rental.tests import test_order_api as orders
from core.timezones import zone_for

call, booking = orders.call, orders.booking
world, token, fresh_throttle, shop = orders.world, orders.token, orders.fresh_throttle, orders.shop

LOGIN = "/api/v1/operator/auth/login"


def login(client):
    return call(client, LOGIN, {"username": "OPR001", "password": orders.fare_api.PASSWORD,
                                "installation_id": "till-1"}).json()["data"]


def now_text(world):
    return timezone.localtime(timezone.now(), zone_for(world["company"])).strftime("%Y-%m-%d %H:%M:%S")


def test_the_booking_stores_it_as_sent_and_answers_with_it(client, world, token, shop):
    body = call(client, orders.BOOK, booking(world, shop, transaction_no="ADC1-A-7"), token=token).json()
    assert body["code"] == "ok", body
    assert body["data"]["transaction_no"] == "ADC1-A-7"
    assert Order.objects.get().transaction_no == "ADC1-A-7"


def test_it_is_optional(client, world, token, shop):
    body = call(client, orders.BOOK, booking(world, shop), token=token).json()
    assert (body["code"], body["data"]["transaction_no"]) == ("ok", "")


def test_login_gives_the_prefix_and_todays_last_number(client, world, shop):
    adc1 = world["adc1"]
    prefix = devices_services.bill_prefix_for(adc1)
    data = login(client)                     # each login replaces the session: use its own token
    assert (data["transaction_no_prefix"], data["last_transaction_no"]) == (prefix, None)   # single device
    # The fixture's booking is on 2 Oct -- not today's.
    call(client, orders.BOOK, booking(world, shop, transaction_no="X-1"), token=data["tokens"]["access"])
    data = login(client)
    assert data["last_transaction_no"] is None
    today = booking(world, shop, number=232, transaction_no="X-2", booked_at=now_text(world),
                    start_time=now_text(world))
    today["items"] = [{**today["items"][0], "vehicle_id": shop["mo42"].pk}]
    assert call(client, orders.BOOK, today, token=data["tokens"]["access"]).json()["code"] == "ok"
    assert login(client)["last_transaction_no"] == "X-2"

    # A multi-device station with a letter on this tablet's mapping.
    adc1.is_multi_device = True
    adc1.save()
    DeviceMapping.objects.filter(device=shop["tablet"], to_date__isnull=True).update(transaction_letter="B")
    assert login(client)["transaction_no_prefix"] == f"{prefix}-B"


def test_it_shows_on_the_web_and_in_live_monitoring(client, world, token, shop):
    call(client, orders.BOOK, booking(world, shop, transaction_no="ADC1-9"), token=token)
    order = Order.objects.get()
    assert live.row(order, zone_for(world["company"]))["transaction_no"] == "ADC1-9"

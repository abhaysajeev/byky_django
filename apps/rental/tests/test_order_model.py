"""Order and OrderItem as the database keeps them: the money columns it
computes (paid, balance, payment status), the order number's key, and the
one-active-line-per-vehicle rule."""

import uuid
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.company.models import PaymentMode
from apps.devices.models import Device, DeviceStatus
from apps.fleet.models import UOM, Brand, Category, Vehicle, VehicleType
from apps.rental.models import (
    Order,
    OrderAction,
    OrderEvent,
    OrderItem,
    OrderItemStatus,
    PaymentKind,
    PaymentStatus,
)
from apps.rental.services import OrderRefused, lock_order, record_payment, run_once
from apps.rental.tests.conftest import make_customer
from core.enums import Channel
from core.ids import uuid7
from core.models import User


@pytest.fixture
def make_order(world):
    customer = make_customer(world)
    device = Device.objects.create(
        company=world["company"], installation_id="till-1", platform="android", channel=Channel.OPERATOR,
        status=DeviceStatus.APPROVED, approved_at=timezone.now(),
    )

    def make(order_no=None, company=None, **fields):
        now = timezone.now()
        values = {
            "id": uuid7(), "company": company or world["company"], "branch": world["adc1"], "device": device,
            "customer": customer, "order_no": order_no or f"C1-{uuid.uuid4().hex[:6]}",
            "booked_at": now, "start_time": now, "total_amount": "100.00", "net_amount": "105.00",
        }
        values.update(fields)
        order = Order.objects.create(**values)
        order.refresh_from_db()
        return order

    return make


@pytest.mark.parametrize(("received", "refunded", "net", "paid", "balance", "status"), [
    ("0", "0", "105.00", "0.00", "105.00", PaymentStatus.UNPAID),
    ("50", "0", "105.00", "50.00", "55.00", PaymentStatus.PARTLY_PAID),
    ("105", "0", "105.00", "105.00", "0.00", PaymentStatus.PAID),
    ("110", "5", "105.00", "105.00", "0.00", PaymentStatus.PAID),
    ("110", "0", "105.00", "110.00", "-5.00", PaymentStatus.PAID),        # overpaid: refund owed
    ("0", "0", "0.00", "0.00", "0.00", PaymentStatus.PAID),               # a free order
    ("105", "105", "105.00", "0.00", "105.00", PaymentStatus.UNPAID),     # refunded in full
], ids=["unpaid", "partly", "paid", "paid-after-refund", "overpaid", "free", "refunded"])
def test_the_database_computes_paid_balance_and_payment_status(
        make_order, received, refunded, net, paid, balance, status):
    order = make_order(amount_received=received, amount_refunded=refunded, net_amount=net)
    assert (order.paid_amount, order.balance_due, order.payment_status) == (
        Decimal(paid), Decimal(balance), status)


def test_payment_status_follows_a_new_payment(make_order):
    order = make_order()
    Order.objects.filter(pk=order.pk).update(amount_received=Decimal("105.00"))
    order.refresh_from_db()
    assert (order.balance_due, order.payment_status) == (Decimal("0.00"), PaymentStatus.PAID)


def test_the_order_number_is_unique_per_company(make_order, world):
    make_order(order_no="C1-0007-000231")
    with pytest.raises(IntegrityError), transaction.atomic():
        make_order(order_no="C1-0007-000231")
    make_order(order_no="C1-0007-000231", company=world["other"])     # another company's series


def test_a_vehicle_is_on_one_active_line_at_a_time(make_order, world):
    company = world["company"]
    category = Category.objects.create(company=company, category_code="C", category_name="Bikes")
    brand = Brand.objects.create(company=company, brand_code="B", brand_name="Byky")
    vtype = VehicleType.objects.create(company=company, category=category, brand=brand, vehicle_type_name="Bike")
    uom = UOM.objects.create(company=company, uom_code="N", uom_name="Number")
    bike = Vehicle.objects.create(company=company, vehicle_code="MO41", vehicle_name="MO 41",
                                  vehicle_type=vtype, uom=uom, current_branch=world["adc1"])
    now = timezone.now()

    def line(order, **fields):
        return OrderItem.objects.create(
            id=uuid7(), order=order, vehicle=bike, package_minutes=60, start_time=now, expected_end_time=now,
            rate="50", amount="50", total_amount="50", **fields)

    first = line(make_order())
    with pytest.raises(IntegrityError), transaction.atomic():
        line(make_order())
    first.status = OrderItemStatus.RETURNED
    first.save()
    assert line(make_order()).created_on is not None                  # free again, and audited


# -- Payment entries ---------------------------------------------------------------


def test_payment_entries_move_the_orders_totals(make_order, world):
    cash = PaymentMode.objects.create(company=world["company"], name="Cash")
    card = PaymentMode.objects.create(company=world["company"], name="Card")
    order = make_order(net_amount="104.00")
    tablet_time = timezone.now() - timezone.timedelta(hours=2)      # booked offline, synced later

    def pay(kind, mode, amount, **extra):
        record_payment(order, payment_id=uuid7(), kind=kind, mode=mode, amount=Decimal(amount),
                       paid_at=tablet_time, **extra)
        order.refresh_from_db()
        return (order.amount_received, order.amount_refunded, order.paid_amount, order.balance_due,
                order.payment_status)

    assert pay(PaymentKind.ADVANCE, cash, "60") == (60, 0, 60, 44, PaymentStatus.PARTLY_PAID)
    assert pay(PaymentKind.ADVANCE, card, "50", reference_no="4421") == (110, 0, 110, -6, PaymentStatus.PAID)
    assert pay(PaymentKind.REFUND, cash, "6") == (110, 6, 104, 0, PaymentStatus.PAID)

    entries = list(order.payments.order_by("created_on").values_list("kind", "mode__name", "amount", "paid_at"))
    assert entries == [
        (PaymentKind.ADVANCE, "Cash", Decimal("60.00"), tablet_time),
        (PaymentKind.ADVANCE, "Card", Decimal("50.00"), tablet_time),
        (PaymentKind.REFUND, "Cash", Decimal("6.00"), tablet_time),
    ]


def test_a_payment_entry_needs_a_positive_amount(make_order, world):
    cash = PaymentMode.objects.create(company=world["company"], name="Cash")
    order = make_order()
    with pytest.raises(IntegrityError), transaction.atomic():
        record_payment(order, payment_id=uuid7(), kind=PaymentKind.ADVANCE, mode=cash, amount=Decimal("0"),
                       paid_at=timezone.now())
    order.refresh_from_db()
    assert order.amount_received == 0          # the total did not move either


# -- One call, once (run_once) ---------------------------------------------------------


@pytest.fixture
def operator(world):
    return User.objects.create_user("opr.1", "Byky#2026", display_name="Operator", company=world["company"])


def run(world, operator, order, event_id, request_data, *, company=None, calls=None):
    """run_once with an apply() that records each time it really runs."""
    calls = calls if calls is not None else []

    def apply():
        calls.append(event_id)
        event = {"action": OrderAction.PAYMENT, "happened_at": timezone.now(), "user": operator}
        return order, event, {"order_no": order.order_no, "calls": len(calls)}

    return run_once(event_id=event_id, company=company or world["company"], request_data=request_data,
                    apply=apply)


def test_a_call_runs_once_and_a_resend_gets_the_same_reply(make_order, world, operator):
    order, event_id, calls = make_order(), uuid7(), []

    first = run(world, operator, order, event_id, {"a": 1, "b": [1, 2]}, calls=calls)
    again = run(world, operator, order, event_id, {"b": [1, 2], "a": 1}, calls=calls)     # keys reordered

    assert first == ({"order_no": order.order_no, "calls": 1}, True)
    assert again == ({"order_no": order.order_no, "calls": 1}, False)
    assert len(calls) == 1 and OrderEvent.objects.get().response == first[0]


@pytest.mark.parametrize("other_company", [False, True], ids=["other-body", "other-company"])
def test_the_same_call_id_for_something_else_is_a_conflict(make_order, world, operator, other_company):
    order, event_id = make_order(), uuid7()
    run(world, operator, order, event_id, {"a": 1})

    with pytest.raises(OrderRefused) as refused:
        run(world, operator, order, event_id, {"a": 1} if other_company else {"a": 2},
            company=world["other"] if other_company else None)

    assert (refused.value.code, refused.value.status, refused.value.retry) == ("sync_id_conflict", 409, False)


def test_a_refused_call_writes_nothing(make_order, world):
    order, cash = make_order(), PaymentMode.objects.create(company=world["company"], name="Cash")

    def apply():
        record_payment(order, payment_id=uuid7(), kind=PaymentKind.ADVANCE, mode=cash, amount=Decimal("10"),
                       paid_at=timezone.now())
        raise OrderRefused("unknown_customer")

    with pytest.raises(OrderRefused):
        run_once(event_id=uuid7(), company=world["company"], request_data={}, apply=apply)

    order.refresh_from_db()
    assert (OrderEvent.objects.count(), order.payments.count(), order.amount_received) == (0, 0, 0)


def test_a_constraint_lost_in_a_race_is_its_own_refusal(make_order, world):
    """The checks before a write cannot close every race; the database's own
    rule is the last word, and it is reported as what it means."""
    taken = make_order(order_no="C1-000231")

    def apply():
        make_order(order_no=taken.order_no)          # another tablet got the number first
        raise AssertionError("not reached")

    with pytest.raises(OrderRefused) as refused:
        run_once(event_id=uuid7(), company=world["company"], request_data={}, apply=apply)

    assert refused.value.code == "order_no_used"


def test_an_order_not_on_the_server_yet_is_retryable(world):
    with pytest.raises(OrderRefused) as refused, transaction.atomic():
        lock_order(uuid7(), world["adc1"])

    assert (refused.value.code, refused.value.retry) == ("order_not_synced", True)

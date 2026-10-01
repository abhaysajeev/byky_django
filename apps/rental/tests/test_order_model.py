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
    OrderStatus,
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
            "booked_at": now, "start_time": now,
        }
        values.update(fields)
        order = Order.objects.create(**values)
        order.refresh_from_db()
        return order

    return make


def test_an_order_on_rent_has_no_bill_only_the_money_taken(make_order):
    order = make_order(amount_received="60", amount_refunded="0")

    assert (order.subtotal, order.net_amount, order.balance_due) == (None, None, None)
    assert (order.paid_amount, order.payment_status) == (Decimal("60.00"), PaymentStatus.PENDING)


@pytest.mark.parametrize(("received", "refunded", "net", "paid", "balance"), [
    ("105", "0", "105.00", "105.00", "0.00"),
    ("110", "5", "105.00", "105.00", "0.00"),           # overpaid, the difference refunded
    ("100", "0", "105.00", "100.00", "5.00"),           # what a settle would refuse
], ids=["paid", "refunded-difference", "short"])
def test_once_billed_the_database_computes_the_balance(make_order, received, refunded, net, paid, balance):
    order = make_order(amount_received=received, amount_refunded=refunded, net_amount=net)

    assert (order.paid_amount, order.balance_due) == (Decimal(paid), Decimal(balance))


@pytest.mark.parametrize(("status", "payment_status"), [
    (OrderStatus.ACTIVE, PaymentStatus.PENDING),
    (OrderStatus.COMPLETED, PaymentStatus.PAID),
    (OrderStatus.CANCELLED, None),
])
def test_payment_status_follows_the_order(make_order, status, payment_status):
    order = make_order()
    Order.objects.filter(pk=order.pk).update(status=status)
    order.refresh_from_db()

    assert order.payment_status == payment_status


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
            base_fare="50", **fields)

    first = line(make_order())
    with pytest.raises(IntegrityError), transaction.atomic():
        line(make_order())
    first.status, first.overtime_amount, first.total_amount = OrderItemStatus.RETURNED, 0, 50
    first.save()
    assert line(make_order()).created_on is not None                  # free again, and audited


# -- Payment entries ---------------------------------------------------------------


def test_payment_entries_move_the_orders_totals(make_order, world):
    cash = PaymentMode.objects.create(company=world["company"], name="Cash")
    card = PaymentMode.objects.create(company=world["company"], name="Card")
    order = make_order(net_amount="104.00")         # billed, to see the balance move
    tablet_time = timezone.now() - timezone.timedelta(hours=2)      # booked offline, synced later

    def pay(kind, mode, amount, **extra):
        record_payment(order, payment_id=uuid7(), kind=kind, mode=mode, amount=Decimal(amount),
                       paid_at=tablet_time, **extra)
        order.refresh_from_db()
        return (order.amount_received, order.amount_refunded, order.paid_amount, order.balance_due,
                order.payment_status)

    assert pay(PaymentKind.ADVANCE, cash, "60") == (60, 0, 60, 44, PaymentStatus.PENDING)
    assert pay(PaymentKind.ADVANCE, card, "50", reference_no="4421") == (110, 0, 110, -6, PaymentStatus.PENDING)
    assert pay(PaymentKind.REFUND, cash, "6") == (110, 6, 104, 0, PaymentStatus.PENDING)

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

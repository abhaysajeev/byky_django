"""The order rebuild converts what is already on a database in place.

0005_order_lifecycle: the renames keep their values, the received / refunded
totals come from the payment entries, blank or repeated order numbers get a
unique stand-in before the unique key goes on. 0006_payment_entry: `rental`
payments become `balance`, collected_at becomes paid_at, reference_date a
date.

Each test leaves the database migrated to the latest state again, or every
test after it in the same worker would run on the old schema."""

import uuid
from decimal import Decimal

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

BEFORE = [("rental", "0004_order_payment")]
AFTER = [("rental", "0006_payment_entry")]


def migrate(targets):
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    return executor.loader.project_state(targets).apps


@pytest.fixture(autouse=True)
def back_to_latest():
    yield
    executor = MigrationExecutor(connection)
    migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
def test_orders_survive_the_rebuild():
    apps = migrate(BEFORE)
    Country, State, Location, Company, Branch = (
        apps.get_model("company", name) for name in ("Country", "State", "Location", "Company", "Branch")
    )
    Device = apps.get_model("devices", "Device")
    PaymentMode = apps.get_model("company", "PaymentMode")
    Customer, Order, OrderItem, Payment = (
        apps.get_model("rental", name) for name in ("Customer", "Order", "OrderItem", "Payment")
    )
    Vehicle = apps.get_model("fleet", "Vehicle")
    Category, Brand, VehicleType, UOM = (
        apps.get_model("fleet", name) for name in ("Category", "Brand", "VehicleType", "UOM")
    )

    uae = Country.objects.create(short_code="AE", name="UAE")
    state = State.objects.create(country=uae, short_code="AUH", name="Abu Dhabi")
    company = Company.objects.create(short_code="BYKY", name="BYKY", country=uae, state=state,
                                     phone_number="+9710", email="a@b.test")
    location = Location.objects.create(country=uae, state=state, short_code="C", name="Corniche")
    branch = Branch.objects.create(company=company, location=location, short_code="C1", name="Corniche 1",
                                   branch_type="station")
    device = Device.objects.create(company=company, installation_id="i-1", platform="android",
                                   channel="operator", status="approved", approved_at=timezone.now())
    cash = PaymentMode.objects.create(company=company, name="Cash")
    customer = Customer.objects.create(company=company, customer_code="CU001", first_name="Ahmed",
                                       mobile_country_code="+971", mobile_no="501234567",
                                       mobile_full="971501234567")
    category = Category.objects.create(company=company, category_code="C", category_name="Bikes")
    brand = Brand.objects.create(company=company, brand_code="B", brand_name="Byky")
    vtype = VehicleType.objects.create(company=company, category=category, brand=brand, vehicle_type_name="Bike")
    uom = UOM.objects.create(company=company, uom_code="N", uom_name="Number")
    vehicle = Vehicle.objects.create(company=company, vehicle_code="MO41", vehicle_name="MO 41",
                                     vehicle_type=vtype, uom=uom, current_branch=branch)

    now = timezone.now()

    def order(order_no, status="active", paid="0"):
        return Order.objects.create(
            id=uuid.uuid4(), company=company, branch=branch, device=device, customer=customer,
            customer_name="Ahmed", customer_mobile="501234567", order_no=order_no, status=status,
            device_created_at=now, start_time=now, number_of_vehicles=1, total_amount="100.00",
            net_amount="105.00", advance_amount="100.00", payment_mode=cash, paid_amount=paid,
        )

    paid = order("C1-1", paid="104.00")
    Payment.objects.create(order=paid, kind="advance", mode=cash, amount="110.00", collected_at=now)
    Payment.objects.create(order=paid, kind="refund", mode=cash, amount="6.00", collected_at=now)
    balance = Payment.objects.create(order=paid, kind="rental", mode=cash, amount="1.00", collected_at=now,
                                     reference_no="4421", reference_date=now)
    OrderItem.objects.create(order=paid, vehicle=vehicle, start_time=now, expected_end_time=now,
                             package_minutes=60, rate="50", amount="50", total_amount="50", remarks="Chain")
    blank = order("", status="payment_pending")
    repeated = order("C1-1")

    apps = migrate(AFTER)
    Order, OrderItem = apps.get_model("rental", "Order"), apps.get_model("rental", "OrderItem")

    paid = Order.objects.get(pk=paid.pk)
    assert (paid.booked_at, paid.order_no, paid.customer_mobile) == (now, "C1-1", "971501234567")
    assert (paid.amount_received, paid.amount_refunded, paid.paid_amount) == (
        Decimal("111.00"), Decimal("6.00"), Decimal("105.00"))
    assert (paid.balance_due, paid.payment_status) == (Decimal("0.00"), "paid")

    balance = apps.get_model("rental", "Payment").objects.get(pk=balance.pk)
    assert (balance.kind, balance.paid_at, balance.reference_date) == ("balance", now, now.date())

    blank = Order.objects.get(pk=blank.pk)
    assert blank.status == "active" and blank.order_no == "X" + blank.pk.hex[:24]
    assert (blank.amount_received, blank.payment_status) == (Decimal("0.00"), "unpaid")
    assert Order.objects.get(pk=repeated.pk).order_no == "X" + repeated.pk.hex[:24]

    item = OrderItem.objects.get(order_id=paid.pk)
    assert (item.reason, item.overtime_amount, item.created_on is not None) == ("Chain", Decimal("0.00"), True)

"""Offers renamed Packages without losing anything (fare 0004, portal 0018).

What was saved as an offer -- with its items, free items, time slabs and an
"offer_price" choice, and an order line pointing at it -- is all there after
the migration under the new names; the database constraints carry the new
names; and a role granted the old Offers screen still has the Packages one.

Each test leaves the database migrated to the latest state again, or every
test after it in the same worker would run on the old schema."""

import datetime
import uuid
from decimal import Decimal

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

# Devices is pinned too: a target only rolls back its own app, so without this
# the old Device model these states build would not match the device table.
DEVICES = ("devices", "0012_device_settings_company")
FARE_BEFORE = [("fare", "0003_offer_offerfreeitem_offerfreeitemtimeslab_offeritem_and_more"),
               ("rental", "0010_item_removed"), DEVICES]
FARE_AFTER = [("fare", "0004_offer_to_package"), ("rental", "0010_item_removed"), DEVICES]
PORTAL_BEFORE = [("portal", "0017_invoice_page")]
PORTAL_AFTER = [("portal", "0018_offer_page_to_package")]


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
def test_an_offer_and_everything_pointing_at_it_survive_as_a_package():
    apps = migrate(FARE_BEFORE)
    Country, State, Location, Company, Branch = (
        apps.get_model("company", name) for name in ("Country", "State", "Location", "Company", "Branch")
    )
    Category, Brand, VehicleType, UOM, Vehicle = (
        apps.get_model("fleet", name) for name in ("Category", "Brand", "VehicleType", "UOM", "Vehicle")
    )
    Offer, OfferItem, OfferFreeItem, OfferFreeItemTimeSlab = (
        apps.get_model("fare", name) for name in ("Offer", "OfferItem", "OfferFreeItem", "OfferFreeItemTimeSlab")
    )
    Customer, Order, OrderItem = (apps.get_model("rental", name) for name in ("Customer", "Order", "OrderItem"))
    Device = apps.get_model("devices", "Device")

    uae = Country.objects.create(short_code="AE", name="UAE")
    state = State.objects.create(country=uae, short_code="AUH", name="Abu Dhabi")
    company = Company.objects.create(short_code="BYKY", name="BYKY", country=uae, state=state,
                                     phone_number="+9710", email="a@b.test")
    location = Location.objects.create(country=uae, state=state, short_code="C", name="Corniche")
    branch = Branch.objects.create(company=company, location=location, short_code="C1", name="Corniche 1",
                                   branch_type="station")
    category = Category.objects.create(company=company, category_code="C", category_name="Bikes")
    brand = Brand.objects.create(company=company, brand_code="B", brand_name="Byky")
    vtype = VehicleType.objects.create(company=company, category=category, brand=brand, vehicle_type_name="Bike")
    uom = UOM.objects.create(company=company, uom_code="N", uom_name="Number")
    bike = Vehicle.objects.create(company=company, vehicle_code="MO41", vehicle_name="MO 41", vehicle_type=vtype,
                                  uom=uom, current_branch=branch)

    offer = Offer.objects.create(
        company=company, offer_code="SUMMER", offer_name="Summer deal", level="branch", branch=branch,
        valid_from=datetime.date(2026, 6, 1), valid_to=datetime.date(2026, 8, 31), promotion_for="quantity",
        inventory_type="vehicle_type", lower_value=Decimal("2"), upper_value=Decimal("5"),
        promotion_type="offer_price", free_or_offer_price="offer_price", time_slab_applicable=True,
    )
    OfferItem.objects.create(offer=offer, vehicle_type=vtype, package_minutes=60, value=Decimal("40.00"))
    OfferFreeItem.objects.create(offer=offer, vehicle_type=vtype, package_minutes=30, value=Decimal("15.00"))
    OfferFreeItemTimeSlab.objects.create(offer=offer, from_time=datetime.time(9), to_time=datetime.time(11),
                                         vehicle_type=vtype, package_minutes=30, value=Decimal("10.00"))
    device = Device.objects.create(company=company, installation_id="i-1", platform="android",
                                   channel="operator", status="approved", approved_at=timezone.now())
    customer = Customer.objects.create(company=company, customer_code="CU001", first_name="Ahmed",
                                       mobile_country_code="+971", mobile_no="501234567", mobile_full="971501234567")
    now = timezone.now()
    order = Order.objects.create(id=uuid.uuid4(), company=company, branch=branch, device=device, customer=customer,
                                 order_no="C1-1", booked_at=now, start_time=now)
    line = OrderItem.objects.create(id=uuid.uuid4(), order=order, vehicle=bike, offer=offer, package_minutes=60,
                                    start_time=now, expected_end_time=now, base_fare=Decimal("40.00"),
                                    created_on=now)

    apps = migrate(FARE_AFTER)
    Package = apps.get_model("fare", "Package")

    package = Package.objects.get(pk=offer.pk)
    assert (package.package_code, package.package_name, package.branch_id) == ("SUMMER", "Summer deal", branch.pk)
    assert (package.promotion_type, package.free_or_package_price) == ("package_price", "package_price")
    assert [(i.package_minutes, i.value) for i in package.items.all()] == [(60, Decimal("40.00"))]
    assert [(f.package_minutes, f.value) for f in package.free_items.all()] == [(30, Decimal("15.00"))]
    assert package.time_slabs.get().from_time == datetime.time(9)
    assert apps.get_model("rental", "OrderItem").objects.get(pk=line.pk).offer_id == package.pk

    # The constraints the code names carry the new names (Postgres's own
    # generated names -- primary key, foreign keys -- are not the code's).
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid IN "
            "('package'::regclass, 'package_item'::regclass, 'package_free_item'::regclass, "
            "'package_free_item_time_slab'::regclass)"
        )
        names = {row[0] for row in cursor.fetchall()}
    assert {
        "uniq_package_code_per_company", "package_valid_dates", "package_value_band", "package_scope_matches_level",
        "package_item_package_range", "package_item_value_not_negative", "package_free_item_package_range",
        "package_free_item_value_not_negative", "package_slab_time_order", "package_slab_date_matches_mode",
        "package_slab_package_range", "package_slab_value_not_negative",
    } <= names
    # ...and so do Postgres's generated ones: a migrated database matches a fresh one.
    with connection.cursor() as cursor:
        cursor.execute("SELECT conname FROM pg_constraint WHERE conname LIKE 'offer%' "
                       "UNION ALL SELECT relname FROM pg_class WHERE relname LIKE 'offer%'")
        assert cursor.fetchall() == []


@pytest.mark.parametrize("new_page_exists", [False, True], ids=["renamed-in-place", "merged-into-new-page"])
@pytest.mark.django_db(transaction=True)
def test_a_role_granted_offers_keeps_the_packages_screen(new_page_exists):
    """Two starting points. A database where only fare.offer exists gets its
    row renamed. One that runs 0011-0017 in the same deploy already has a
    fare.package page (those re-apply the current registry) -- the grant moves
    across and the old page goes."""
    apps = migrate(PORTAL_BEFORE)
    Module, Page, Role, RolePermission = (
        apps.get_model("portal", name) for name in ("Module", "Page", "Role", "RolePermission")
    )
    # The starting point is built here, not assumed: these tests empty every
    # table between runs.
    module = Module.objects.get_or_create(code="fare", defaults={"name": "Fare & Offers", "sort_order": 50})[0]
    Page.objects.filter(code__in=["fare.offer", "fare.package"]).delete()
    actions = ["create", "read", "update", "delete", "print"]
    old = Page.objects.create(code="fare.offer", module=module, name="Offers", url_name="fare-offer-list",
                              actions=actions, sort_order=2, is_active=not new_page_exists)
    if new_page_exists:          # as 0011-0017's sync of the current registry leaves it
        Page.objects.create(code="fare.package", module=module, name="Packages", url_name="fare-package-list",
                            actions=actions, sort_order=2, is_active=True)
    role = Role.objects.create(name="Fare Manager")
    RolePermission.objects.create(role=role, page=old, can_read=True, can_update=True)

    apps = migrate(PORTAL_AFTER)
    Page, RolePermission = apps.get_model("portal", "Page"), apps.get_model("portal", "RolePermission")

    assert not Page.objects.filter(code="fare.offer").exists()
    package = Page.objects.get(code="fare.package")
    assert (package.name, package.is_active) == ("Packages", True)
    grant = RolePermission.objects.get(role_id=role.pk)
    assert (grant.page_id, grant.can_read, grant.can_update, grant.can_delete) == (package.pk, True, True, False)

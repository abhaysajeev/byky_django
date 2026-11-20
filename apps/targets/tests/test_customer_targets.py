"""Customer targets (design/08-targets.md section 9): unique customers on the
orders that count for sales, monthly only, beside a sales target for the same
branch and month."""

import uuid
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.devices.models import Device, DeviceStatus
from apps.rental.models import Order, OrderStatus
from apps.rental.tests.conftest import make_customer
from apps.targets import services
from apps.targets.models import Target, TargetKind
from apps.targets.tests.test_targets import NOW, local, staff, two_decimals  # noqa: F401
from core.enums import Channel
from core.ids import uuid7

pytestmark = pytest.mark.django_db
D = Decimal
CUSTOMERS = TargetKind.CUSTOMERS


@pytest.fixture
def visit(world):
    """visit(day, customer_no, status=COMPLETED, branch=None): one order."""
    device = Device.objects.create(company=world["company"], installation_id="till-c", platform="android",
                                   channel=Channel.OPERATOR, status=DeviceStatus.APPROVED, approved_at=timezone.now())
    people = {}

    def make(day, who, status=OrderStatus.COMPLETED, branch=None):
        if who not in people:
            people[who] = make_customer(world, mobile_no=f"55{who:07d}", customer_code=f"C{who:04d}")
        branch = branch or world["adc1"]
        at = local(day)
        return Order.objects.create(
            id=uuid7(), company=branch.company, branch=branch, device=device, customer=people[who],
            order_no=f"V-{uuid.uuid4().hex[:8]}", booked_at=at, start_time=at, status=status,
            net_amount=D("10") if status == OrderStatus.COMPLETED else None)

    return make


def customer_target(world, staff, amount="30", now=NOW):  # noqa: F811
    saved, refused = services.create_targets(world["company"], [world["adc1"]], 2026, 11, D(amount), "", staff,
                                             now=now, kind=CUSTOMERS)
    assert not refused
    return saved[0]


def test_each_customer_counts_once_and_cancelled_orders_not_at_all(world, visit):
    visit(3, 1)
    visit(4, 1)                                       # the same customer again
    visit(5, 2)
    visit(5, 3, status=OrderStatus.CANCELLED)          # incl. a voided invoice
    visit(5, 4, status=OrderStatus.ACTIVE)             # not settled yet
    counts = services.customers_by_branch(world["company"], [world["adc1"].pk], local(1).date(), local(30).date())
    assert counts == {world["adc1"].pk: 2}


def test_a_customer_target_has_no_days_and_sits_beside_the_sales_target(world, staff):  # noqa: F811
    services.create_targets(world["company"], [world["adc1"]], 2026, 11, D("30000"), "", staff, now=NOW)
    target = customer_target(world, staff)
    assert target.days.count() == 0
    assert Target.objects.filter(branch=world["adc1"]).count() == 2
    _, refused = services.create_targets(world["company"], [world["adc1"]], 2026, 11, D("40"), "", staff,
                                         now=NOW, kind=CUSTOMERS)
    assert [why for _, why in refused] == ["it already has a target for this month"]


def test_on_track_is_pro_rata(world, staff, visit):  # noqa: F811
    target = customer_target(world, staff, amount="30")      # 30 in 30 days; 9 days before 10 Nov
    for who in range(1, 10):
        visit(2, who)
    view = services.month_view(target, now=NOW)
    assert (view["figures"].achieved, view["status"]["key"], view["days"]) == (D("9"), "track", [])
    visit(3, 99, branch=world["adc1"])
    assert services.month_view(target, now=NOW)["figures"].achieved == D("10")


def test_the_overview_counts_customers_not_money(world, staff, visit):  # noqa: F811
    customer_target(world, staff, amount="5")
    for who in range(1, 7):
        visit(4, who)
    view = services.overview([world["adc1"]], 2026, 11, now=NOW, kind=CUSTOMERS)
    row = view["rows"][0]
    assert (row["figures"].achieved, row["status"]["key"], row["today"]) == (D("6"), "met", None)
    assert view["totals"]["remaining"] == D("0")


def test_an_edit_changes_only_the_count(world, staff):  # noqa: F811
    target = customer_target(world, staff)
    services.update_target(target, D("45"), "Winter", staff, now=NOW)
    target.refresh_from_db()
    assert (target.amount, target.remarks, target.days.count()) == (D("45"), "Winter", 0)


def test_the_pages_switch_to_customers(client_in, world, staff):  # noqa: F811
    today = services.today_for(world["company"])
    saved, _ = services.create_targets(world["company"], [world["adc1"]], today.year, today.month, D("120"), "",
                                       staff, kind=CUSTOMERS)
    target = saved[0]
    for url in (reverse("targets-overview") + "?kind=customers",
                reverse("targets-overview") + f"?profile={target.profile_id}",
                reverse("targets-target", args=[target.pk])):
        response = client_in.get(url)
        assert response.status_code == 200, url
        assert "120" in response.content.decode() and "120.00" not in response.content.decode(), url


def test_the_drawer_saves_a_whole_number_of_customers(client_in, world):
    month = f"{services.today_for(world['company']):%Y-%m}"
    save = lambda **d: client_in.post(reverse("targets-save"), d, content_type="application/json")  # noqa: E731
    bad = save(branches=str(world["adc1"].pk), month=month, kind="customers", amount="10.5")
    good = save(branches=str(world["adc1"].pk), month=month, kind="customers", amount="250")
    assert bad.status_code == 400 and "whole number" in bad.json()["errors"][0]["message"]
    assert good.json()["ok"] is True
    assert Target.objects.get().kind == CUSTOMERS

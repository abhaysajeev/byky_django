"""Targets (design/08-targets.md): the split, the business day, achieved,
required and its freezing, edit rules, and the screens."""

import datetime
import uuid
import zoneinfo
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.company.models import Branch, BranchType, CompanyConfig, CompanyMoneySetting
from apps.devices.models import Device, DeviceStatus
from apps.portal.models import Role
from apps.rental.models import (
    CreditNote,
    CreditNoteSource,
    CreditNoteStatus,
    Invoice,
    Order,
    OrderStatus,
)
from apps.rental.tests.conftest import PASSWORD, make_customer
from apps.targets import services
from apps.targets.models import Target, TargetDay
from core.enums import Channel
from core.ids import uuid7
from core.models import User
from core.timezones import business_day_of

pytestmark = pytest.mark.django_db

DUBAI = zoneinfo.ZoneInfo("Asia/Dubai")
NOW = datetime.datetime(2026, 11, 10, 10, 0, tzinfo=DUBAI)       # business day 10 Nov
D = Decimal


def local(day, hour=10, minute=0):
    return datetime.datetime(2026, 11, day, hour, minute, tzinfo=DUBAI)


@pytest.fixture(autouse=True)
def two_decimals(request):
    """The UAE's money: 2 decimals (Configurations), as on QA."""
    if "world" not in request.fixturenames:
        return
    world = request.getfixturevalue("world")
    CompanyMoneySetting.objects.create(company=world["company"], country=world["company"].country,
                                       decimal_places=2, round_off_step=D("0.25"))


@pytest.fixture
def day_start(world):
    CompanyConfig.objects.create(company=world["company"], report_day_start=datetime.time(4, 0))


@pytest.fixture
def staff(world):
    return User.objects.create_user("kripa.o", PASSWORD, display_name="Kripa", company=world["company"])


@pytest.fixture
def sale(world, staff):
    customer = make_customer(world)
    device = Device.objects.create(company=world["company"], installation_id="till-1", platform="android",
                                   channel=Channel.OPERATOR, status=DeviceStatus.APPROVED, approved_at=timezone.now())

    def make(at, net, status=OrderStatus.COMPLETED, branch=None, credit=None):
        branch = branch or world["adc1"]
        order = Order.objects.create(
            id=uuid7(), company=branch.company, branch=branch, device=device, customer=customer,
            order_no=f"T-{uuid.uuid4().hex[:8]}", booked_at=at, start_time=at, status=status,
            net_amount=D(net) if status == OrderStatus.COMPLETED or net else None)
        if credit is not None:
            invoice = Invoice.objects.create(
                id=uuid7(), order=order, company=order.company, branch=branch, device=device, issued_by=staff,
                invoice_no=order.order_no, issued_at=at, company_name="BYKY", branch_name=branch.name,
                subtotal=D(net), discount_amount=0, tax_percentage=5, tax_amount=0, rounding_adjustment=0,
                net_amount=D(net))
            CreditNote.objects.create(
                id=uuid7(), company=order.company, branch=branch, order=order, invoice=invoice,
                source=CreditNoteSource.WEB, status=CreditNoteStatus.APPROVED, credit_note_no=f"{order.order_no}CN",
                net_amount=D(credit), taxable_amount=D(credit), tax_amount=0, tax_percentage=5, decided_at=at)
        return order

    return make


def make_target(world, staff, amount="30000", now=NOW):
    saved, refused = services.create_targets(world["company"], [world["adc1"]], 2026, 11, D(amount), "", staff,
                                             now=now)
    assert not refused
    return saved[0]


# -- The split and the business day -----------------------------------------------------------


def test_the_split_adds_up_with_the_remainder_on_the_last_day():
    days = services.days_of(datetime.date(2026, 11, 1), datetime.date(2026, 11, 30))
    split = services.split_even(D("1000"), days, 2)

    assert split[days[0]] == D("33.33") and split[days[-1]] == D("33.43")
    assert sum(split.values()) == D("1000")


def test_a_sale_before_the_day_start_belongs_to_the_day_before(world, day_start):
    assert business_day_of(world["company"], local(2, 1, 30)) == datetime.date(2026, 11, 1)
    assert business_day_of(world["company"], local(2, 4, 0)) == datetime.date(2026, 11, 2)


def test_without_a_day_start_the_calendar_day_applies(world):
    assert business_day_of(world["company"], local(2, 1, 30)) == datetime.date(2026, 11, 2)


# -- Achieved -------------------------------------------------------------------------------


def test_achieved_is_net_less_credit_notes_on_the_booked_day(world, day_start, sale):
    sale(local(1), "100", credit="20")
    sale(local(2, 1, 30), "50")                                  # before 04:00: still 1 Nov
    sale(local(1, 12), "70", status=OrderStatus.CANCELLED)       # cancelled: never counts
    sale(local(1, 13), None, status=OrderStatus.ACTIVE)          # running: not yet
    theirs = Branch.objects.get(pk=world["theirs"].pk)
    sale(local(1, 14), "999", branch=theirs)                     # another station

    got = services.achieved_by_day(world["company"], [world["adc1"].pk], datetime.date(2026, 11, 1),
                                   datetime.date(2026, 11, 30))

    assert got == {(world["adc1"].pk, datetime.date(2026, 11, 1)): D("130")}


# -- Creating, required and freezing ---------------------------------------------------------


def test_a_new_target_plans_every_day_and_freezes_up_to_today(world, staff, sale):
    sale(local(1), "600")
    target = make_target(world, staff)
    days = list(target.days.order_by("date"))

    assert len(days) == 30 and days[0].planned_amount == D("1000")
    assert days[0].required_amount == D("1000")
    assert days[1].required_amount == D("1013.79")               # (30,000 - 600) / 29
    assert days[9].required_amount is not None and days[10].required_amount is None


def test_a_frozen_required_amount_never_moves(world, staff, sale):
    target = make_target(world, staff)
    second = TargetDay.objects.get(target=target, date=datetime.date(2026, 11, 2))
    assert second.required_amount == D("1034.48")                # (30,000 - 0) / 29
    sale(local(1), "5000")                                       # arrives late
    services.freeze_due(now=NOW)

    second.refresh_from_db()
    assert second.required_amount == D("1034.48")


def test_freeze_due_fills_the_new_day_and_is_idempotent(world, staff, sale):
    target = make_target(world, staff)
    sale(local(10), "3000")
    tomorrow = NOW + datetime.timedelta(days=1)

    assert services.freeze_due(now=tomorrow) == 1
    assert services.freeze_due(now=tomorrow) == 0
    eleventh = TargetDay.objects.get(target=target, date=datetime.date(2026, 11, 11))
    assert eleventh.required_amount == D("1350")                 # (30,000 - 3,000) / 20 days left


def test_required_is_never_below_zero(world, staff, sale):
    sale(local(1), "40000")
    target = make_target(world, staff)

    assert TargetDay.objects.get(target=target, date=datetime.date(2026, 11, 2)).required_amount == D("0")


def test_a_station_with_a_target_for_the_month_is_refused_by_name(world, staff):
    make_target(world, staff)
    closed = Branch.objects.create(company=world["company"], location=world["adc1"].location, short_code="ADC9",
                                   name="Closed Gate", branch_type=BranchType.STATION, is_active=False)
    fresh = Branch.objects.create(company=world["company"], location=world["adc1"].location, short_code="ADC2",
                                  name="Corniche 2", branch_type=BranchType.STATION)

    saved, refused = services.create_targets(world["company"], [world["adc1"], closed, fresh], 2026, 11,
                                             D("100"), "", staff, now=NOW)

    assert [t.branch for t in saved] == [fresh]
    assert {b.name for b, _ in refused} == {world["adc1"].name, "Closed Gate"}


def test_an_ended_month_can_not_get_a_target(world, staff):
    with pytest.raises(services.TargetError):
        services.create_targets(world["company"], [world["adc1"]], 2026, 10, D("100"), "", staff, now=NOW)


# -- Editing ---------------------------------------------------------------------------------


def test_a_mid_month_edit_keeps_past_days_and_reshares_the_rest(world, staff):
    target = make_target(world, staff)
    services.update_target(target, D("41000"), "Eid", staff, now=NOW)
    days = list(target.days.order_by("date"))

    assert all(d.planned_amount == D("1000") for d in days[:9])
    assert days[9].planned_amount == D("1523.80")                # (41,000 - 9,000) / 21, rounded down
    assert sum(d.planned_amount for d in days) == D("41000")
    assert days[9].required_amount == D("1952.38")               # today: 41,000 / 21 left


def test_an_ended_month_is_locked(world, staff):
    target = make_target(world, staff)
    december = datetime.datetime(2026, 12, 2, 10, tzinfo=DUBAI)

    with pytest.raises(services.TargetError):
        services.update_target(target, D("1"), "", staff, now=december)
    with pytest.raises(services.TargetError):
        services.delete_target(target, now=december)


def test_the_month_view_figures(world, staff, sale):
    sale(local(1), "600")
    sale(local(10), "200")
    view = services.month_view(make_target(world, staff), now=NOW)

    assert (view["figures"].achieved, view["figures"].remaining, view["days_left"]) == (D("800"), D("29200"), 21)
    assert view["today"]["achieved"] == D("200") and view["days"][0]["state"] == "missed"
    assert view["days"][15]["state"] == "future"


# -- Screens ---------------------------------------------------------------------------------


def _current_target(world, staff):
    today = services.today_for(world["company"])
    saved, _ = services.create_targets(world["company"], [world["adc1"]], today.year, today.month, D("9000"), "",
                                       staff)
    return saved[0]


def test_the_screens_open(client_in, world, staff):
    target = _current_target(world, staff)
    first = target.period_start
    urls = [reverse("targets-list"), reverse("targets-branch", args=[world["adc1"].pk]),
            reverse("targets-month", args=[world["adc1"].pk, first.year, first.month]),
            reverse("targets-month", args=[world["adc1"].pk, first.year + 1, first.month]),
            reverse("targets-privileges"), reverse("targets-month-data", args=[target.pk])]
    for url in urls:
        assert client_in.get(url).status_code == 200, url
    assert client_in.get(reverse("targets-month-data", args=[target.pk])).json()["amount"] == "9000.00"


def test_another_companys_station_is_not_reachable(client_in, world):
    assert client_in.get(reverse("targets-branch", args=[world["theirs"].pk])).status_code == 404


def _save(client, **data):
    return client.post(reverse("targets-save"), data, content_type="application/json")


def test_add_edit_and_delete_from_the_drawer(client_in, world):
    today = services.today_for(world["company"])
    month = f"{today:%Y-%m}"

    first = _save(client_in, branches=[str(world["adc1"].pk)], month=month, amount="12000", remarks="")
    again = _save(client_in, branches=[str(world["adc1"].pk)], month=month, amount="12000")
    target = Target.objects.get(branch=world["adc1"])
    edit = _save(client_in, pk=target.pk, amount="15000.50", remarks="Eid")
    bad = _save(client_in, pk=target.pk, amount="-1")
    gone = client_in.post(reverse("targets-delete", args=[target.pk]))

    assert first.json()["ok"] is True
    assert again.status_code == 400 and "already has a target" in again.json()["errors"][0]["message"]
    assert edit.json()["ok"] is True
    assert bad.status_code == 400
    assert gone.json()["ok"] is True and not Target.objects.exists()


def test_a_role_without_targets_is_refused(client, world):
    role = Role.objects.create(company=world["company"], name="Cashier")
    User.objects.create_user("no.targets", PASSWORD, display_name="No", company=world["company"], role=role,
                             allowed_channels=[Channel.WEB])
    client.post("/login/", {"username": "no.targets", "password": PASSWORD})

    assert client.get(reverse("targets-list")).status_code == 403
    assert _save(client, branches=[str(world["adc1"].pk)], month="2026-12", amount="1").status_code == 403


def test_configurations_saves_the_target_period(client_in, world):
    response = client_in.post(reverse("company-configurations-save"),
                              {"company": world["company"].pk, "report_day_start": "04:00", "money": [],
                               "target_period": "monthly"}, content_type="application/json")
    wrong = client_in.post(reverse("company-configurations-save"),
                           {"company": world["company"].pk, "target_period": "weekly"},
                           content_type="application/json")

    assert response.json()["ok"] is True
    assert CompanyConfig.objects.get(company=world["company"]).target_period == "monthly"
    assert wrong.status_code == 400

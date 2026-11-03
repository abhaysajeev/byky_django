"""Configurations (CR1, 3 Nov 2026): the page, its save, money per country,
the report window, and core/money.py."""

import datetime
import zoneinfo
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apps.company.models import (
    Branch,
    BranchType,
    CompanyConfig,
    CompanyMoneySetting,
    Country,
    Location,
    State,
)
from apps.company.tests import test_crud
from apps.portal.models import RolePermission
from apps.reports.filters import Filters
from core import money

world, client_in, post = test_crud.world, test_crud.client_in, test_crud.post

PAGE = "/company/configurations/"
SAVE = "/company/configurations/save/"
DUBAI = zoneinfo.ZoneInfo("Asia/Dubai")


def uae(world):
    return world["company"].country


def row(country, places=2, mode="upward", step="0.25"):
    return {"country": country.pk, "decimal_places": places, "round_off_mode": mode, "round_off_step": step}


# -- the page and its save -----------------------------------------------------------


def test_the_page_opens_on_the_users_company(client_in, world):
    response = client_in.get(PAGE)
    assert response.status_code == 200
    assert response.context["company"] == world["company"]


def test_save_stores_the_day_start_and_money(client_in, world):
    body = post(client_in, SAVE, {"report_day_start": "04:00", "money": [row(uae(world))]}).json()
    assert body["ok"] is True
    assert CompanyConfig.objects.get(company=world["company"]).report_day_start == datetime.time(4, 0)
    setting = CompanyMoneySetting.objects.get(company=world["company"])
    assert (setting.decimal_places, setting.round_off_mode, setting.round_off_step) == (2, "upward", Decimal("0.25"))


def test_a_save_replaces_the_rows(client_in, world):
    kuwait = Country.objects.create(short_code="KW", name="Kuwait")
    State.objects.create(country=kuwait, short_code="KWC", name="Kuwait City")
    CompanyMoneySetting.objects.create(company=world["company"], country=kuwait, decimal_places=3)
    post(client_in, SAVE, {"money": [row(uae(world))]})
    assert list(CompanyMoneySetting.objects.filter(company=world["company"]).values_list("country", flat=True)) == [
        uae(world).pk]


@pytest.mark.parametrize("bad, message", [
    ({"decimal_places": 4}, "Decimals must be 2 or 3."),
    ({"round_off_step": "0.005"}, "Choose a round-off step for 2 decimals."),
    ({"round_off_mode": "sideways"}, "Choose a round-off mode."),
])
def test_a_bad_row_is_refused(client_in, world, bad, message):
    body = post(client_in, SAVE, {"money": [{**row(uae(world)), **bad}]}).json()
    assert body["ok"] is False and body["errors"][0]["message"] == message


def test_a_country_twice_is_refused(client_in, world):
    body = post(client_in, SAVE, {"money": [row(uae(world)), row(uae(world), places=3, step="0.250")]}).json()
    assert body["errors"][0]["message"] == "Each country can have one row only."


def test_a_bad_time_is_refused(client_in, world):
    assert post(client_in, SAVE, {"report_day_start": "25:99", "money": []}).json()["ok"] is False


def test_save_needs_update(client_in, world):
    RolePermission.objects.filter(role=client_in.role, page__code="company.configurations").update(can_update=False)
    assert post(client_in, SAVE, {"money": [row(uae(world))]}).status_code == 403


def test_another_company_cannot_be_chosen(client_in, world):
    post(client_in, SAVE, {"company": world["other"].pk, "money": [row(uae(world))]})
    assert not CompanyMoneySetting.objects.filter(company=world["other"]).exists()


# -- which money setting applies -----------------------------------------------------------


def test_a_station_follows_its_own_country(world):
    kuwait = Country.objects.create(short_code="KW", name="Kuwait")
    state = State.objects.create(country=kuwait, short_code="KWC", name="Kuwait City")
    place = Location.objects.create(country=kuwait, state=state, short_code="HIL", name="Hilton")
    station = Branch.objects.create(company=world["company"], location=place, short_code="KW1", name="Hilton",
                                    branch_type=BranchType.STATION)
    CompanyMoneySetting.objects.create(company=world["company"], country=uae(world), decimal_places=2,
                                       round_off_step=Decimal("0.25"))
    CompanyMoneySetting.objects.create(company=world["company"], country=kuwait, decimal_places=3)
    assert money.money_for(world["company"]).decimal_places == 2
    assert money.money_for(world["company"], station).decimal_places == 3


def test_no_row_keeps_three_decimals(world):
    assert money.money_for(world["company"]) == money.DEFAULT


# -- core/money.py -----------------------------------------------------------------------------


def test_fmt_and_quantize():
    assert money.fmt(Decimal("75"), 2) == "75.00"
    assert money.fmt(Decimal("2.345"), 2) == "2.35"            # half up
    assert money.fmt(Decimal("75.5"), 3) == "75.500"
    assert money.fmt(None, 2) is None


def test_check_places_finds_nested_values():
    data = {"amount": Decimal("1.50"), "items": [{"fare": Decimal("1.555")}]}
    assert money.check_places(data, 2) == [("items[0].fare", Decimal("1.555"))]
    assert money.check_places({"a": Decimal("75.500")}, 2) == []      # trailing zeros are fine


@pytest.mark.parametrize("net, adjustment, mode, ok", [
    ("75.25", "0.15", "upward", True),
    ("75.25", "-0.10", "upward", False),
    ("75.10", "0.00", "upward", False),        # not a multiple of 0.25
    ("75.00", "-0.10", "downward", True),
    ("75.00", "0.10", "downward", False),
    ("75.00", "0.12", "nearest", True),
    ("75.00", "0.13", "nearest", False),
])
def test_check_round_off(net, adjustment, mode, ok):
    setting = money.Money(2, mode, Decimal("0.25"))
    assert (money.check_round_off(Decimal(net), Decimal(adjustment), setting) is None) is ok


# -- the report window ---------------------------------------------------------------------------

REPORT = SimpleNamespace(filters=(), statuses=(), dated=True)


@pytest.mark.parametrize("now_hour", [10, 2])      # after 04:00, and before it: the same window
def test_a_day_start_opens_on_yesterday_at_that_time(now_hour):
    now = datetime.datetime(2026, 11, 3, now_hour, 30, tzinfo=DUBAI)
    f = Filters({}, REPORT, DUBAI, now, day_start=datetime.time(4, 0))
    assert (f.start, f.end) == (datetime.datetime(2026, 11, 2, 4, 0), datetime.datetime(2026, 11, 3, 3, 59))
    assert f.until == datetime.datetime(2026, 11, 3, 4, 0, tzinfo=DUBAI)


def test_without_a_day_start_the_calendar_day():
    now = datetime.datetime(2026, 11, 3, 10, 0, tzinfo=DUBAI)
    f = Filters({}, REPORT, DUBAI, now)
    assert (f.start, f.end) == (datetime.datetime(2026, 11, 3, 0, 0), datetime.datetime(2026, 11, 3, 23, 59))


def test_a_chosen_range_wins_over_the_day_start():
    now = datetime.datetime(2026, 11, 3, 10, 0, tzinfo=DUBAI)
    f = Filters({"from": "2026-11-01T00:00", "to": "2026-11-01T23:59"}, REPORT, DUBAI, now,
                day_start=datetime.time(4, 0))
    assert f.start == datetime.datetime(2026, 11, 1, 0, 0)


# -- a system user follows the only company (owner, 3 Nov 2026) --------------------------------


def test_a_system_user_follows_the_only_company(world):
    from apps.company.models import Company
    from core.enums import UserScope
    from core.models import User

    admin = User.objects.create_user("root.admin", "Byky#2026", scope=UserScope.SYSTEM)
    assert money.company_for_user(admin) is None              # two companies: no single answer
    Company.objects.filter(pk=world["other"].pk).delete()
    assert money.company_for_user(admin) == world["company"]


def test_a_company_user_keeps_their_own_company(world):
    from core.models import User

    user = User.objects.create_user("own.user", "Byky#2026", company=world["other"])
    assert money.company_for_user(user) == world["other"]

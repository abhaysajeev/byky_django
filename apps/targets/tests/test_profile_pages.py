"""Target Profiles screens: saving a profile through the form, refusals by
name, the searchable Branches control, locked fields and delete rules."""

import datetime
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.company.models import Branch, BranchType
from apps.targets import services
from apps.targets.models import PeriodType, Target, TargetProfile
from apps.targets.tests.test_targets import two_decimals  # noqa: F401

pytestmark = pytest.mark.django_db
D = Decimal


def _save(client, **data):
    return client.post(reverse("targets-profile-save"), data, content_type="application/json")


def _next_month(world):
    today = services.today_for(world["company"])
    first = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
    return first, (first + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(days=1)


@pytest.fixture
def creek(world):
    return Branch.objects.create(company=world["company"], location=world["adc1"].location, short_code="CRK",
                                 name="Creek Park", branch_type=BranchType.STATION)


def test_saving_an_active_profile_makes_its_targets(client_in, world, creek):
    first, last = _next_month(world)
    response = _save(client_in, name="Next month", metric="sales", period_type="monthly",
                     starts_on=first.isoformat(), ends_on=last.isoformat(), status="active",
                     branches=[{"id": world["adc1"].pk, "amount": "30,000"}, {"id": creek.pk, "amount": "12000"}])

    body = response.json()
    assert body["ok"] is True and "2 targets made" in body["message"]
    profile = TargetProfile.objects.get(pk=body["pk"])
    assert dict(profile.targets.values_list("branch__name", "amount")) == {
        world["adc1"].name: D("30000"), "Creek Park": D("12000")}


def test_a_clash_is_refused_by_branch_name_and_the_rest_saved(client_in, world, creek):
    first, last = _next_month(world)
    common = {"metric": "sales", "period_type": "monthly", "starts_on": first.isoformat(),
              "ends_on": last.isoformat(), "status": "active"}
    _save(client_in, name="Base", branches=[{"id": world["adc1"].pk, "amount": "1000"}], **common)
    body = _save(client_in, name="Second", branches=[{"id": world["adc1"].pk, "amount": "5"},
                                                     {"id": creek.pk, "amount": "5"}], **common).json()

    assert body["ok"] is True
    assert [r["branch"] for r in body["refused"]] == [world["adc1"].name]
    assert Target.objects.filter(profile_id=body["pk"]).get().branch == creek


def test_the_form_checks_its_fields(client_in, world):
    bad = _save(client_in, name="", metric="hours", period_type="weekly", starts_on="2026-12-10",
                ends_on="2026-12-01", status="active", branches=[]).json()
    fields = {e["field"] for e in bad["errors"]}
    assert {"Name", "Metric", "Type", "Branches"} <= fields

    annual = _save(client_in, name="FY", metric="customers", period_type="annual", starts_on="2027-01-01",
                   ends_on="2027-12-31", status="draft",
                   branches=[{"id": world["adc1"].pk, "amount": "10.5"}],
                   weights=dict.fromkeys(map(str, range(1, 13)), "8")).json()
    assert {e["field"] for e in annual["errors"]} == {world["adc1"].name, "Month weights"}


def test_type_and_metric_lock_once_targets_exist(client_in, world):
    first, last = _next_month(world)
    pk = _save(client_in, name="Locked", metric="sales", period_type="monthly", starts_on=first.isoformat(),
               ends_on=last.isoformat(), status="active",
               branches=[{"id": world["adc1"].pk, "amount": "100"}]).json()["pk"]
    again = _save(client_in, pk=pk, name="Locked", metric="customers", period_type="daily",
                  starts_on=first.isoformat(), ends_on=last.isoformat(), status="active",
                  branches=[{"id": world["adc1"].pk, "amount": "100"}]).json()

    assert {e["field"] for e in again["errors"]} == {"Metric", "Type"}
    page = client_in.get(reverse("targets-profile-edit", args=[pk])).content.decode()
    assert 'data-tp="metric" disabled' in page


def test_the_branches_control_is_a_searchable_multi_select(client_in, world):
    page = client_in.get(reverse("targets-profile-add")).content.decode()
    assert 'class="byky-multi" id="tp-branches"' in page and "data-searchable" in page
    assert 'id="tp-branches-label">Branches<' in page
    assert world["adc1"].name in page


def test_a_plain_multi_select_has_no_search(client_in):
    from django.template.loader import render_to_string

    html = render_to_string("byky/partials/multi_select.html",
                            {"multi_id": "x", "field": "x", "options": [{"id": 1, "name": "One"}], "chosen": []})
    assert "data-searchable" not in html


def test_a_future_profile_can_be_deleted_a_started_one_cannot(client_in, world):
    first, last = _next_month(world)
    today = services.today_for(world["company"])
    future = _save(client_in, name="Future", metric="sales", period_type="monthly", starts_on=first.isoformat(),
                   ends_on=last.isoformat(), status="active",
                   branches=[{"id": world["adc1"].pk, "amount": "100"}]).json()["pk"]
    running = _save(client_in, name="Running", metric="sales", period_type="custom",
                    starts_on=today.isoformat(), ends_on=today.isoformat(), status="active",
                    branches=[{"id": world["adc1"].pk, "amount": "100"}]).json()["pk"]

    assert client_in.post(reverse("targets-profile-delete", args=[future])).json()["ok"] is True
    refused = client_in.post(reverse("targets-profile-delete", args=[running]))
    assert refused.status_code == 400 and TargetProfile.objects.filter(pk=running).exists()


def test_the_screens_show_every_profile_and_the_daily_card(client_in, world, creek):
    today = services.today_for(world["company"])
    first, last = today.replace(day=1), today
    monthly = _save(client_in, name="This month", metric="sales", period_type="custom",
                    starts_on=first.isoformat(), ends_on=(first + datetime.timedelta(days=40)).isoformat(),
                    status="active", branches=[{"id": world["adc1"].pk, "amount": "9000"}]).json()["pk"]
    special = _save(client_in, name="Special day", metric="sales", period_type="daily",
                    starts_on=last.isoformat(), ends_on=last.isoformat(), status="active",
                    branches=[{"id": world["adc1"].pk, "amount": "500"}]).json()["pk"]

    station = client_in.get(reverse("targets-station", args=[world["adc1"].pk])).content.decode()
    assert "This month" in station and "Special day" in station
    card = Target.objects.get(profile_id=special)
    page = client_in.get(reverse("targets-target", args=[card.pk])).content.decode()
    assert "Still to achieve" in page and "scr-x-daycard" in page
    overview = client_in.get(reverse("targets-overview") + f"?profile={monthly}").content.decode()
    assert world["adc1"].name in overview
    assert TargetProfile.objects.get(pk=special).period_type == PeriodType.DAILY

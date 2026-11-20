"""Target Profiles screens: the profile form (one Active switch), Target
Branch Mapping (searchable, Select all), refusals by name, locked fields, and
deleting -- only once no branch is mapped (owner, 20 Nov 2026)."""

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


def _profile(client, **data):
    data.setdefault("metric", "sales")
    return client.post(reverse("targets-profile-save"), data, content_type="application/json")


def _map(client, profile, branches):
    return client.post(reverse("targets-mapping-save"), {"profile": profile, "branches": branches},
                       content_type="application/json")


def _next_month(world):
    today = services.today_for(world["company"])
    first = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
    return first, (first + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(days=1)


def _monthly(client, world, name, active=True):
    first, last = _next_month(world)
    return _profile(client, name=name, period_type="monthly", starts_on=first.isoformat(), ends_on=last.isoformat(),
                    is_active=active).json()


@pytest.fixture
def creek(world):
    return Branch.objects.create(company=world["company"], location=world["adc1"].location, short_code="CRK",
                                 name="Creek Park", branch_type=BranchType.STATION)


def test_a_new_profile_goes_on_to_its_mapping_and_mapping_makes_the_targets(client_in, world, creek):
    body = _monthly(client_in, world, "Next month")
    assert body["ok"] and body["redirect"] == f"{reverse('targets-mapping')}?profile={body['pk']}"
    assert "Map its branches" in body["message"] and not Target.objects.exists()

    mapped = _map(client_in, body["pk"], [{"id": world["adc1"].pk, "amount": "30,000"},
                                          {"id": creek.pk, "amount": "12000"}]).json()
    assert mapped["ok"] and "2 targets made" in mapped["message"]
    profile = TargetProfile.objects.get(pk=body["pk"])
    assert dict(profile.targets.values_list("branch__name", "amount")) == {
        world["adc1"].name: D("30000"), "Creek Park": D("12000")}


def test_a_switched_off_profile_maps_but_makes_nothing(client_in, world):
    body = _monthly(client_in, world, "Later", active=False)
    mapped = _map(client_in, body["pk"], [{"id": world["adc1"].pk, "amount": "100"}]).json()
    assert mapped["ok"] and "switched off" in mapped["message"] and not Target.objects.exists()
    page = client_in.get(reverse("targets-mapping") + f"?profile={body['pk']}").content.decode()
    assert "is switched off" in page


def test_a_clash_is_refused_by_branch_name_and_the_rest_saved(client_in, world, creek):
    base = _monthly(client_in, world, "Base")["pk"]
    _map(client_in, base, [{"id": world["adc1"].pk, "amount": "1000"}])
    second = _monthly(client_in, world, "Second")["pk"]
    body = _map(client_in, second, [{"id": world["adc1"].pk, "amount": "5"}, {"id": creek.pk, "amount": "5"}]).json()

    assert body["ok"] is True
    assert [r["branch"] for r in body["refused"]] == [world["adc1"].name]
    assert Target.objects.filter(profile_id=second).get().branch == creek


def test_the_forms_check_their_fields(client_in, world):
    bad = _profile(client_in, name="", metric="hours", period_type="weekly", starts_on="2026-12-10",
                   ends_on="2026-12-01").json()
    assert {"Name", "Metric", "Type"} <= {e["field"] for e in bad["errors"]}

    annual = _profile(client_in, name="FY", metric="customers", period_type="annual", starts_on="2027-01-01",
                      ends_on="2027-12-31", weights=dict.fromkeys(map(str, range(1, 13)), "8")).json()
    assert {e["field"] for e in annual["errors"]} == {"Month weights"}

    customers = _profile(client_in, name="Cust", metric="customers", period_type="monthly",
                         starts_on="2027-01-01", ends_on="2027-01-31").json()["pk"]
    whole = _map(client_in, customers, [{"id": world["adc1"].pk, "amount": "10.5"}]).json()
    assert [e["field"] for e in whole["errors"]] == [world["adc1"].name]
    stranger = _map(client_in, customers, [{"id": world["theirs"].pk, "amount": "10"}]).json()
    assert [e["field"] for e in stranger["errors"]] == ["Branches"]


def test_type_and_metric_lock_once_targets_exist(client_in, world):
    first, last = _next_month(world)
    pk = _monthly(client_in, world, "Locked")["pk"]
    _map(client_in, pk, [{"id": world["adc1"].pk, "amount": "100"}])
    again = _profile(client_in, pk=pk, name="Locked", metric="customers", period_type="daily",
                     starts_on=first.isoformat(), ends_on=last.isoformat(), is_active=True).json()

    assert {e["field"] for e in again["errors"]} == {"Metric", "Type"}
    page = client_in.get(reverse("targets-profile-edit", args=[pk])).content.decode()
    assert 'data-tp="metric" disabled' in page and 'data-tp="is_active"' in page


def test_the_mapping_picker_is_searchable_with_select_all(client_in, world):
    pk = _monthly(client_in, world, "Pick")["pk"]
    page = client_in.get(reverse("targets-mapping") + f"?profile={pk}").content.decode()
    assert 'class="byky-multi" id="tm-branches"' in page
    assert "data-searchable" in page and "data-select-all" in page
    assert 'id="tm-branches-label">Branches<' in page and world["adc1"].name in page
    form = client_in.get(reverse("targets-profile-edit", args=[pk])).content.decode()
    assert 'id="tp-branches"' not in form and "Target Branch Mapping" in form


def test_a_plain_multi_select_has_no_search_or_select_all(client_in):
    from django.template.loader import render_to_string

    html = render_to_string("byky/partials/multi_select.html",
                            {"multi_id": "x", "field": "x", "options": [{"id": 1, "name": "One"}], "chosen": []})
    assert "data-searchable" not in html and "data-select-all" not in html


def test_delete_is_refused_while_mapped_and_allowed_once_unmapped(client_in, world):
    pk = _monthly(client_in, world, "Mapped")["pk"]
    _map(client_in, pk, [{"id": world["adc1"].pk, "amount": "100"}])

    refused = client_in.post(reverse("targets-profile-delete", args=[pk]))
    assert refused.status_code == 400 and "mapped to 1 branch" in refused.json()["errors"][0]["message"]

    _map(client_in, pk, [])                                         # unmap it
    gone = client_in.post(reverse("targets-profile-delete", args=[pk])).json()
    assert gone["ok"] and not TargetProfile.objects.filter(pk=pk).exists() and not Target.objects.exists()


def test_a_running_profile_once_unmapped_goes_with_its_history(client_in, world):
    today = services.today_for(world["company"])
    pk = _profile(client_in, name="Running", period_type="custom", starts_on=today.isoformat(),
                  ends_on=today.isoformat(), is_active=True).json()["pk"]
    _map(client_in, pk, [{"id": world["adc1"].pk, "amount": "100"}])
    _map(client_in, pk, [])
    assert Target.objects.filter(profile_id=pk).exists()            # started: kept as history while it exists
    assert client_in.post(reverse("targets-profile-delete", args=[pk])).json()["ok"]
    assert not Target.objects.filter(profile_id=pk).exists()


def test_the_list_has_a_kebab_with_mapping_and_delete(client_in, world):
    pk = _monthly(client_in, world, "Listed")["pk"]
    page = client_in.get(reverse("targets-profiles")).content.decode()
    assert f'data-pk="{pk}"' in page and "data-scr-delete" in page and "Branch mapping" in page
    assert 'data-scr-delete-url="/targets/profiles/0/delete/"' in page
    off = client_in.get(reverse("targets-profiles") + "?show=off").content.decode()
    assert "Listed" not in off


def test_the_screens_show_every_profile_and_the_daily_card(client_in, world, creek):
    today = services.today_for(world["company"])
    first = today.replace(day=1)
    monthly = _profile(client_in, name="This month", period_type="custom", starts_on=first.isoformat(),
                       ends_on=(first + datetime.timedelta(days=40)).isoformat(), is_active=True).json()["pk"]
    special = _profile(client_in, name="Special day", period_type="daily", starts_on=today.isoformat(),
                       ends_on=today.isoformat(), is_active=True).json()["pk"]
    _map(client_in, monthly, [{"id": world["adc1"].pk, "amount": "9000"}])
    _map(client_in, special, [{"id": world["adc1"].pk, "amount": "500"}])

    station = client_in.get(reverse("targets-station", args=[world["adc1"].pk])).content.decode()
    assert "This month" in station and "Special day" in station
    card = Target.objects.get(profile_id=special)
    page = client_in.get(reverse("targets-target", args=[card.pk])).content.decode()
    assert "Still to achieve" in page and "scr-x-daycard" in page
    overview = client_in.get(reverse("targets-overview") + f"?profile={monthly}").content.decode()
    assert world["adc1"].name in overview
    assert TargetProfile.objects.get(pk=special).period_type == PeriodType.DAILY


def test_mapping_needs_its_own_right(client, world):
    from apps.portal.models import Role
    from apps.rental.tests.conftest import PASSWORD
    from core.enums import Channel
    from core.models import User

    role = Role.objects.create(company=world["company"], name="Cashier")
    User.objects.create_user("no.mapping", PASSWORD, display_name="No", company=world["company"], role=role,
                             allowed_channels=[Channel.WEB])
    client.post("/login/", {"username": "no.mapping", "password": PASSWORD})
    assert client.get(reverse("targets-mapping")).status_code == 403
    assert _map(client, 1, []).status_code == 403

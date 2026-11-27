"""Owner, 26 Nov 2026:

* "Include regular discounts" on every fare season and special price, and on
  every package -- default off; plain data the fare and package downloads
  carry to the tablet (the server checks nothing). A fare's own base price has
  no flag.
* A package row's vehicle type and package minutes are picked from existing
  fares: only a fare in force within the package's dates, at its scope.
"""

import pytest

from apps.fare import package_services, services
from apps.fare.models import Package
from apps.fare.package_services import Invalid
from apps.fare.tests.conftest import D, make_fare
from apps.fare.tests.test_package_services import data as package_data
from apps.fare.tests.test_package_services import item, slab
from apps.fare.tests.test_services import data as fare_data
from apps.fare.tests.test_services import days, every
from core.enums import ApprovalStatus

LIVE = {"approval_status": ApprovalStatus.APPROVED, "is_active": True}


# -- The flag on fares ---------------------------------------------------------------------


def test_the_flag_saves_on_rules_and_seasons_and_defaults_off(world, user):
    fare, _ = services.save_fare(user, fare_data(world, rules=[
        days("r1", [6, 0, 1, 2, 3], "10:00", "13:00", 70, include_regular_discounts=True),
        every("r2", "16:00", "20:00", 60),
    ], seasons=[
        {"key": "s1", "name": "Winter", "start_date": "2026-10-01", "end_date": "2026-12-31",
         "base": {"base_fare": "55", "grace_minutes": 5}, "include_regular_discounts": True,
         "rules": [every("r3", "10:00", "12:00", 45)]},
        {"key": "s2", "name": "Summer", "start_date": "2026-05-01", "end_date": "2026-09-30",
         "base": {"base_fare": "40", "grace_minutes": 5}, "rules": []},
    ]))

    rules = {r.start_minute: r.include_regular_discounts for r in fare.rules.filter(season__isnull=True)}
    assert rules == {600: True, 960: False}                     # the season's own rule is checked below
    seasons = {s.name: s.include_regular_discounts for s in fare.seasons.all()}
    assert seasons == {"Winter": True, "Summer": False}
    winter = fare.seasons.get(name="Winter")
    assert fare.rules.get(season=winter).include_regular_discounts is False


def test_the_edit_screen_and_the_download_carry_it(world, user):
    fare, _ = services.save_fare(user, fare_data(world, rules=[
        every("r1", "10:00", "12:00", 45, include_regular_discounts=True)]))
    from apps.fare import payload

    edit = payload.serialise(services.with_children(type(fare).objects.filter(pk=fare.pk)).get())
    assert edit["rules"][0]["include_regular_discounts"] is True
    assert "include_regular_discounts" not in edit["base"]          # the base price has none

    device = services._device_fare(services.with_children(type(fare).objects.filter(pk=fare.pk)).get())
    assert device["special_prices"][0]["include_regular_discounts"] is True
    assert "include_regular_discounts" not in device["price"]


# -- The flag on packages ------------------------------------------------------------------


@pytest.fixture
def fares(world):
    """Monaco 30 min, company-wide over 2026; Berg 15 min, at ADC1 only, from July."""
    return {
        "monaco30": make_fare(world, **LIVE),
        "berg15_adc1": make_fare(world, vehicle_type=world["berg"], package_minutes=15, branches=[world["adc1"]],
                                 valid_from=D(2026, 7, 1), **LIVE),
    }


def test_a_package_saves_the_flag_default_off(world, user, fares):
    off = package_services.save_package(user, package_data(world))
    on = package_services.save_package(user, package_data(world, package_code="ON", include_regular_discounts=True))

    assert (off.include_regular_discounts, on.include_regular_discounts) == (False, True)
    from apps.fare import package_device, package_payload
    assert package_payload.serialise(on)["include_regular_discounts"] is True
    assert package_device._package_json(on)["include_regular_discounts"] is True


# -- Rows pick existing fares ----------------------------------------------------------------


def test_a_row_without_a_fare_is_refused(world, user, fares):
    with pytest.raises(Invalid) as refused:
        package_services.save_package(user, package_data(world, items=[item("n1", world["monaco"].pk, package=45)]))
    assert "45 min has no active fare" in refused.value.errors[0]["message"]
    assert not Package.objects.exists()


def test_a_fare_outside_the_package_dates_is_refused(world, user, fares):
    berg = item("n1", world["berg"].pk, package=15)
    with pytest.raises(Invalid):
        package_services.save_package(user, package_data(   # Berg's fare starts 1 Jul
            world, level="branch", branch=world["adc1"].pk, valid_to="2026-06-30", items=[berg]))
    assert package_services.save_package(user, package_data(
        world, level="branch", branch=world["adc1"].pk, valid_from="2026-07-01", items=[berg])).pk


def test_a_branch_fare_fits_its_own_branch_its_location_and_company_packages(world, user, fares):
    berg = [item("n1", world["berg"].pk, package=15)]
    july = {"valid_from": "2026-07-01", "items": berg}
    with pytest.raises(Invalid):                                     # another branch: no Berg fare there
        package_services.save_package(user, package_data(world, level="branch", branch=world["adc2"].pk, **july))
    assert package_services.save_package(user, package_data(
        world, package_code="B", level="branch", branch=world["adc1"].pk, **july)).pk
    assert package_services.save_package(user, package_data(
        world, package_code="L", level="location", location=world["location"].pk, **july)).pk
    assert package_services.save_package(user, package_data(world, package_code="C", **july)).pk


def test_free_items_and_time_slabs_are_checked_too(world, user, fares):
    with pytest.raises(Invalid) as refused:
        package_services.save_package(user, package_data(
            world, promotion_type="quantity", free_item_selectable=True, time_slab_applicable=True,
            items=[item("n1", world["monaco"].pk, value=None)],
            free_items=[{"key": "f1", "vehicle_type": world["berg"].pk, "package_minutes": 30, "value": "1"}],
            time_slabs=[slab("t1", world["berg"].pk, package=30)]))
    assert {e["field"] for e in refused.value.errors} == {"Free Promotion Items", "Free Item Time Slabs"}


def test_inactive_and_unapproved_fares_are_not_offered_or_accepted(world, user, fares):
    make_fare(world, vehicle_type=world["berg"], package_minutes=45, is_active=False,
              approval_status=ApprovalStatus.APPROVED)
    choices = {(c["vehicle_type"], c["package_minutes"]) for c in package_services.fare_choices(user)}
    assert choices == {(world["monaco"].pk, 30), (world["berg"].pk, 15)}
    with pytest.raises(Invalid):
        package_services.save_package(user, package_data(world, items=[item("n1", world["berg"].pk, package=45)]))


def test_the_choices_carry_what_the_picker_filters_by(world, user, fares):
    berg = next(c for c in package_services.fare_choices(user) if c["vehicle_type"] == world["berg"].pk)
    assert berg == {
        "id": fares["berg15_adc1"].pk, "company": world["company"].pk, "vehicle_type": world["berg"].pk,
        "vehicle_type_name": "Berg", "package_minutes": 15, "valid_from": "2026-07-01", "valid_to": "2026-12-31",
        "level": "branch", "branches": [world["adc1"].pk], "locations": [world["location"].pk],
    }


def test_the_page_has_the_fare_picker_not_typed_minutes(client_in, world, fares):
    html = client_in.get("/fare/package/add/").content.decode()
    assert html.count("data-fare-picker=") == 2                     # the item dialog and the slab dialog
    assert "data-item-package data-num" not in html and "data-slab-package data-num" not in html
    assert 'data-package-field="include_regular_discounts"' in html
    assert '"fares": [' in html

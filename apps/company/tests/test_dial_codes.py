"""Dial codes for mobile numbers (owner, 11 Nov 2026): the bundled list, the
loader, and POST /api/v1/{app}/dial-codes for every app."""

import pytest

from apps.company import dial_codes
from apps.company.models import DialCode
from apps.crew.tests import test_attendance as attendance

world, fresh_throttle, call, login = attendance.world, attendance.fresh_throttle, attendance.call, attendance.login
operator, manager = attendance.operator, attendance.manager

URL = "/api/v1/{}/dial-codes"


def by_iso(rows):
    return {row["iso2"]: row for row in rows}


# -- the bundled list ---------------------------------------------------------------------------

def test_the_bundled_list_has_every_country_with_the_right_numbers():
    rows = by_iso(dial_codes.read_file())

    assert len(rows) > 240
    assert rows["AE"] == {"iso2": "AE", "country": "United Arab Emirates", "dial_code": "971",
                          "mobile_digits_min": 9, "mobile_digits_max": 9, "example_mobile": "501234567"}
    assert (rows["US"]["dial_code"], rows["US"]["mobile_digits_min"]) == ("1", 10)
    assert (rows["DE"]["mobile_digits_min"], rows["DE"]["mobile_digits_max"]) == (10, 11)
    assert rows["KW"]["dial_code"] == "965" and rows["IN"]["mobile_digits_max"] == 10
    assert all(row["country"] and row["country"] != row["iso2"] for row in rows.values())


def test_the_bundled_list_is_what_the_packages_give_now():
    pytest.importorskip("phonenumbers")
    pytest.importorskip("babel")

    assert dial_codes.build() == dial_codes.read_file()


def test_flags_are_built_from_the_iso_code():
    assert dial_codes.flag("AE") == "\U0001f1e6\U0001f1ea"
    assert dial_codes.flag("in") == "\U0001f1ee\U0001f1f3"


# -- the table ----------------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_migration_filled_the_table_with_the_uae_first():
    first = list(DialCode.objects.order_by("sort_order", "country").values_list("iso2", flat=True)[:6])

    assert first == list(dial_codes.FIRST)
    uae = DialCode.objects.get(iso2="AE")
    assert (uae.dial_code, uae.dial_code_digits, uae.flag) == ("971", 3, "\U0001f1e6\U0001f1ea")


@pytest.mark.django_db
def test_a_reload_keeps_what_a_person_changed_and_fixes_the_rest():
    DialCode.objects.filter(iso2="IN").update(is_active=False, sort_order=7, country="Wrong")

    created, updated = dial_codes.load(DialCode, dial_codes.read_file())

    india = DialCode.objects.get(iso2="IN")
    assert (created, updated) == (0, 1)
    assert (india.is_active, india.sort_order, india.country) == (False, 7, "India")


@pytest.mark.django_db
def test_a_reload_with_nothing_new_changes_nothing():
    assert dial_codes.load(DialCode, dial_codes.read_file()) == (0, 0)


# -- the API ------------------------------------------------------------------------------------

def test_the_operator_app_gets_active_countries_in_order(client, world, operator):
    DialCode.objects.filter(iso2="FR").update(is_active=False)

    body = call(client, URL.format("operator"), token=operator).json()

    assert body["code"] == "ok", body
    rows = body["data"]["dial_codes"]
    assert [row["iso2"] for row in rows[:6]] == list(dial_codes.FIRST)
    assert rows[0] == {"iso2": "AE", "country": "United Arab Emirates", "flag": "\U0001f1e6\U0001f1ea",
                       "dial_code": "+971", "dial_code_digits": 3, "mobile_digits_min": 9,
                       "mobile_digits_max": 9, "example": "501234567"}
    assert "FR" not in by_iso(rows)
    assert body["data"]["version"]


def test_the_version_moves_when_the_list_changes(client, world, operator):
    before = call(client, URL.format("operator"), token=operator).json()["data"]["version"]
    uae = DialCode.objects.get(iso2="AE")
    uae.sort_order = 2
    uae.save()

    after = call(client, URL.format("operator"), token=operator).json()["data"]["version"]

    assert after > before


def test_the_manager_app_gets_it_too(client, world, manager):
    body = call(client, URL.format("manager"), token=manager).json()

    assert body["code"] == "ok" and body["data"]["dial_codes"][0]["iso2"] == "AE"


def test_a_token_from_another_app_is_refused(client, world, operator):
    assert call(client, URL.format("manager"), token=operator).json()["code"] == "wrong_channel"


def test_no_sign_in_no_list(client, world):
    response = call(client, URL.format("operator"))

    assert response.status_code == 401


def test_it_is_in_the_api_docs(client, db):
    schema = client.get("/api/schema/").content.decode()

    assert "/api/v1/{app}/dial-codes" in schema

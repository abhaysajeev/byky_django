"""POST /api/v1/{app}/app/update-check, tested through HTTP as the app sees it.

The lookup itself is covered in test_app_release.py. These tests hold the
contract: the envelope, the public access, the type checks, the fail-open rule
and the rate limit. design/03-login.md section 9A; handout
design/registration/update-check-api.md.
"""

import logging

import pytest
from django.core.cache import cache

from apps.devices import api, services
from apps.devices.models import UpdateType
from apps.devices.tests.test_app_release import map_to, open_all_day, release, tablet

URL = "/api/v1/operator/app/update-check"


@pytest.fixture(autouse=True)
def fresh_throttle():
    """The throttle counts in the cache, which outlives a test."""
    cache.clear()
    yield
    cache.clear()


def call(client, request_data=None, *, url=URL, credentials=None, body=None):
    if body is None:
        body = {"credentials": credentials or {}, "request_data": request_data or {}}
    return client.post(url, body, content_type="application/json")


def ask(client, installation_id="new-tablet", **version):
    return call(client, {"installation_id": installation_id, **version})


def assert_envelope(response):
    body = response.json()
    assert set(body) == {"code", "message", "data"}
    return body


# -- The foundation every device API shares ----------------------------------


def test_no_token_is_needed(client, company):
    """The project default is JWT + IsAuthenticated. The update check runs
    before login, so a 401 here would stop every tablet on its first call."""
    response = ask(client, version_code=1)

    assert response.status_code == 200
    assert assert_envelope(response)["code"] == "up_to_date"


@pytest.mark.parametrize("app", ["operator", "manager", "employee"])
def test_each_phone_app_has_the_endpoint(client, company, app):
    response = call(client, {"installation_id": "x", "version_code": 1},
                    url=f"/api/v1/{app}/app/update-check")
    assert response.status_code == 200


@pytest.mark.parametrize("url", [
    "/api/v1/web/app/update-check",       # the web signs in through the screens
    "/api/v1/nonsense/app/update-check",
    "/api/v1/operator/app/no-such-call",
])
def test_an_unknown_app_or_call_is_404_in_the_envelope(client, url):
    response = call(client, {"installation_id": "x"}, url=url)

    assert response.status_code == 404
    assert assert_envelope(response)["code"] == "not_found"


def test_a_get_is_405_in_the_envelope(client):
    response = client.get(URL)

    assert response.status_code == 405
    assert assert_envelope(response)["code"] == "method_not_allowed"


def test_a_body_that_is_not_json_is_400_in_the_envelope(client):
    response = client.post(URL, "{not json", content_type="application/json")

    assert response.status_code == 400
    assert assert_envelope(response)["code"] == "invalid_request"


def test_a_crash_still_answers_in_the_envelope(client, company, monkeypatch):
    """A bug must reach the app as JSON it can read, never an HTML page."""
    def boom(**kwargs):
        raise RuntimeError("bug")
    monkeypatch.setattr(services, "update_decision_for_installation", boom)

    response = ask(client, version_code=1)

    assert response.status_code == 500
    body = assert_envelope(response)
    assert body["code"] == "server_error"
    assert "bug" not in body["message"]


def test_the_credentials_block_is_accepted_whatever_it_holds(client, company):
    """Never validated: the app sends its standard block on every call."""
    credentials = {
        "device_id": "", "token_id": None, "company_id": "999", "imei": ["x"],
        "latitude": "not a number", "send_status": {"nested": True},
    }
    response = call(client, {"installation_id": "x", "version_code": 1}, credentials=credentials)

    assert response.status_code == 200


def test_a_malformed_credentials_block_is_ignored(client, company):
    response = call(client, body={"credentials": "junk", "request_data": {
        "installation_id": "x", "version_code": 1,
    }})

    assert response.status_code == 200


# -- request_data: types only ------------------------------------------------


def errors_of(response):
    assert response.status_code == 400
    body = assert_envelope(response)
    assert body["code"] == "invalid_request"
    return body["message"], body["data"]["errors"]


def test_installation_id_is_required(client):
    message, errors = errors_of(call(client, {"version_code": 1}))

    assert errors == {"installation_id": "is required"}
    assert message == "installation_id is required."


def test_a_missing_request_data_block_reports_the_field(client):
    _, errors = errors_of(call(client, body={"credentials": {}}))

    assert "installation_id" in errors


def test_request_data_must_be_an_object(client):
    _, errors = errors_of(call(client, body={"request_data": ["x"]}))

    assert errors == {"request_data": "must be an object"}


def test_the_body_must_be_an_object(client):
    _, errors = errors_of(call(client, body=[1, 2]))

    assert errors == {"body": "must be a JSON object"}


@pytest.mark.parametrize("bad", ["abc", "76.5", -1, True, [76]])
def test_version_code_must_be_a_whole_number(client, bad):
    message, errors = errors_of(ask(client, version_code=bad))

    assert "version_code" in errors
    assert message.startswith("version_code ")


def test_an_oversized_installation_id_is_refused(client):
    _, errors = errors_of(ask(client, installation_id="x" * 65, version_code=1))

    assert errors == {"installation_id": "must be at most 64 characters"}


def test_an_oversized_version_name_is_refused(client):
    _, errors = errors_of(ask(client, version_name="1." * 20))

    assert "version_name" in errors


def test_unknown_request_keys_are_ignored(client, company):
    response = call(client, {
        "installation_id": "x", "version_code": 1, "hierarchy_id": 12, "username": "sara",
    })

    assert response.status_code == 200


# -- What the tablet is told -------------------------------------------------


def test_a_mandatory_release_answers_update_required(client, company, branch):
    map_to(release(company, 81, update_type=UpdateType.MANDATORY), branch)
    tablet(company, "dubai-tablet", station=branch)

    body = assert_envelope(ask(client, "dubai-tablet", version_code=80))

    assert body["code"] == "update_required"
    assert body["data"] == {
        "update_required": True, "is_mandatory": True,
        "version_code": 81, "version_name": "1.12.81", "update_note": "",
        "download_url": "https://files.byky.test/apk/operator-1.12.81.apk",
    }


def test_an_any_time_release_answers_update_available(client, company, branch):
    map_to(release(company, 81))
    tablet(company, "dubai-tablet", station=branch)
    open_all_day(branch)

    body = assert_envelope(ask(client, "dubai-tablet", version_code=80))

    assert body["code"] == "update_available"
    assert body["data"]["is_mandatory"] is False


@pytest.mark.parametrize("installed", [81, 90])
def test_a_current_or_newer_tablet_is_up_to_date(client, company, installed):
    """Never a downgrade."""
    map_to(release(company, 81, update_type=UpdateType.MANDATORY))

    body = assert_envelope(ask(client, version_code=installed))

    assert body == {"code": "up_to_date", "message": "No update.",
                    "data": {"update_required": False}}


def test_the_name_is_compared_when_no_code_is_sent(client, company):
    map_to(release(company, 81, update_type=UpdateType.MANDATORY))

    assert assert_envelope(ask(client, version_name="1.12.80"))["code"] == "update_required"
    assert assert_envelope(ask(client, version_name="1.12.81"))["code"] == "up_to_date"


def test_a_code_sent_as_text_is_read_as_a_number(client, company):
    map_to(release(company, 81, update_type=UpdateType.MANDATORY))

    assert assert_envelope(ask(client, version_code="80"))["code"] == "update_required"


def test_no_version_at_all_is_up_to_date_and_logged(client, company, caplog):
    """Section 9A.7 row 12: fail open, and log so the app bug is noticed."""
    map_to(release(company, 81, update_type=UpdateType.MANDATORY))

    with caplog.at_level(logging.WARNING, logger="apps.devices.api"):
        body = assert_envelope(ask(client, "forgetful-tablet"))

    assert body["code"] == "up_to_date"
    assert "no version sent" in caplog.text
    assert "forgetful-tablet" in caplog.text


def test_a_lookup_error_is_up_to_date(client, company, monkeypatch):
    """Section 9A.7 row 13: an error inside the lookup fails open."""
    map_to(release(company, 81, update_type=UpdateType.MANDATORY))

    def boom(*args, **kwargs):
        raise RuntimeError("db down")
    monkeypatch.setattr(services, "_company_and_branch", boom)

    assert assert_envelope(ask(client, version_code=1))["code"] == "up_to_date"


def test_an_unknown_tablet_gets_the_companys_all_branches_release(client, company):
    map_to(release(company, 80, update_type=UpdateType.MANDATORY))

    body = assert_envelope(ask(client, "never-seen", version_code=79))

    assert body["data"]["version_code"] == 80


def test_the_app_comes_from_the_url(client, company):
    """An operator release is never offered to the manager app."""
    map_to(release(company, 81))

    response = call(client, {"installation_id": "x", "version_code": 1},
                    url="/api/v1/manager/app/update-check")

    assert assert_envelope(response)["code"] == "up_to_date"


def test_the_client_scenario_over_http(client, company, branch, other_branch):
    """All branches on v80; v81 to Dubai only; Dubai's mapping switched off."""
    dubai, sharjah = branch, other_branch
    tablet(company, "dubai-tablet", station=dubai)
    tablet(company, "sharjah-tablet", station=sharjah)
    open_all_day(dubai, sharjah)
    map_to(release(company, 80))
    dubai_row = map_to(release(company, 81), dubai)

    def offered(installation_id):
        data = assert_envelope(ask(client, installation_id, version_code=79))["data"]
        return data["version_code"]

    assert (offered("dubai-tablet"), offered("sharjah-tablet")) == (81, 80)

    dubai_row.is_active = False
    dubai_row.save(update_fields=["is_active"])
    assert (offered("dubai-tablet"), offered("sharjah-tablet")) == (80, 80)


def test_the_check_changes_nothing(client, company, branch, django_assert_max_num_queries):
    """A pure read -- no write to the device, the mapping or anything else."""
    map_to(release(company, 81), branch)
    tablet(company, "dubai-tablet", station=branch)
    open_all_day(branch)

    with django_assert_max_num_queries(6) as queries:
        ask(client, "dubai-tablet", version_code=80)

    sql = " ".join(q["sql"].upper() for q in queries.captured_queries)
    assert "INSERT" not in sql and "UPDATE " not in sql and "DELETE" not in sql


# -- Rate limit --------------------------------------------------------------


def test_too_many_calls_are_429_in_the_envelope(client, company, monkeypatch):
    monkeypatch.setattr(api.UpdateCheckThrottle, "rate", "2/minute", raising=False)

    ask(client, version_code=1)
    ask(client, version_code=1)
    response = ask(client, version_code=1)

    assert response.status_code == 429
    assert assert_envelope(response)["code"] == "rate_limited"
    assert int(response["Retry-After"]) > 0


# -- "Any time" waits for working hours (03-login.md section 9A.3a) -----------


@pytest.fixture
def sunday_at(monkeypatch):
    """Pin the clock to a Sunday (2026-09-20) at hh:mm, Dubai time."""
    import datetime
    import zoneinfo

    from apps.company import services as company_services

    def pin(hh, mm=0):
        moment = datetime.datetime(2026, 9, 20, hh, mm, tzinfo=zoneinfo.ZoneInfo("Asia/Dubai"))
        monkeypatch.setattr(company_services.timezone, "now", lambda: moment)
    return pin


def sunday_shift(branch, start="08:00", end="16:00"):
    from apps.company.models import BranchWorkingTime, WeekDay

    BranchWorkingTime.objects.create(
        branch=branch, week_day=WeekDay.SUNDAY, shift_number=1, start_time=start, end_time=end,
    )


@pytest.fixture
def placed(company, branch):
    tablet(company, "dubai-tablet", station=branch)
    return branch


def test_any_time_inside_working_hours_is_offered(client, company, placed, sunday_at):
    map_to(release(company, 81))
    sunday_shift(placed)
    sunday_at(10)

    assert assert_envelope(ask(client, "dubai-tablet", version_code=80))["code"] == "update_available"


def test_any_time_outside_working_hours_waits(client, company, placed, sunday_at):
    map_to(release(company, 81))
    sunday_shift(placed)
    sunday_at(20)

    response = ask(client, "dubai-tablet", version_code=80)

    assert response.status_code == 200
    assert assert_envelope(response) == {
        "code": "outside_working_hours",
        "message": "Update available during branch working hours.",
        "data": {"update_required": False},
    }


def test_mandatory_ignores_working_hours(client, company, placed, sunday_at):
    map_to(release(company, 81, update_type=UpdateType.MANDATORY))
    sunday_shift(placed)
    sunday_at(20)

    assert assert_envelope(ask(client, "dubai-tablet", version_code=80))["code"] == "update_required"


def test_no_working_time_is_a_normal_answer(client, company, placed):
    map_to(release(company, 81))

    response = ask(client, "dubai-tablet", version_code=80)

    assert response.status_code == 200
    assert assert_envelope(response) == {
        "code": "working_time_not_set",
        "message": "Branch working time not set.",
        "data": {"update_required": False},
    }


def test_a_tablet_with_no_branch_has_no_working_time(client, company):
    map_to(release(company, 81))

    assert assert_envelope(ask(client, "never-seen", version_code=80))["code"] == "working_time_not_set"


def test_nothing_to_update_is_up_to_date_whatever_the_hours(client, company, placed):
    """The working-time codes only appear when an update is being held back."""
    map_to(release(company, 81))

    assert assert_envelope(ask(client, "dubai-tablet", version_code=81))["code"] == "up_to_date"


def test_a_working_time_error_offers_the_update(client, company, placed, monkeypatch):
    from apps.company import services as company_services

    map_to(release(company, 81))

    def boom(*args, **kwargs):
        raise RuntimeError("db down")
    monkeypatch.setattr(company_services, "branch_open_state", boom)

    assert assert_envelope(ask(client, "dubai-tablet", version_code=80))["code"] == "update_available"


# -- Several companies on one server -----------------------------------------


def test_an_unknown_tablet_names_the_company_whose_build_it_gets(client, company):
    """One server, several companies. A tablet nobody has registered yet has no
    row to read a company from, so it says which one it belongs to -- otherwise
    a new tablet on an old build could never be told about a mandatory release
    (design/03-login.md section 9A.3)."""
    from apps.company.models import Company

    second = Company.objects.create(
        short_code="1001", name="Second", country=company.country, state=company.state,
        phone_number="+9712222222", email="two@byky.test",
    )
    # Mandatory, so the answer is not held back for want of working hours --
    # an unregistered tablet has no branch to have hours.
    map_to(release(company, 90, update_type=UpdateType.MANDATORY), None)
    map_to(release(second, 95, update_type=UpdateType.MANDATORY), None)

    reply = assert_envelope(ask(client, "brand-new", version_code=80, company_id="1001"))

    assert reply["code"] == "update_required"
    assert reply["data"]["version_code"] == 95


def test_a_known_tablet_ignores_the_company_the_app_sends(client, company):
    """Its own row says where it belongs; the app cannot move it."""
    from apps.company.models import Company

    second = Company.objects.create(
        short_code="1001", name="Second", country=company.country, state=company.state,
        phone_number="+9712222222", email="two@byky.test",
    )
    tablet(company, "known-1")
    map_to(release(company, 90, update_type=UpdateType.MANDATORY), None)
    map_to(release(second, 95, update_type=UpdateType.MANDATORY), None)

    reply = assert_envelope(ask(client, "known-1", version_code=80, company_id="1001"))

    assert reply["data"]["version_code"] == 90

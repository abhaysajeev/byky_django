"""POST /api/v1/{app}/server/time, tested through HTTP as the app sees it.

The contract: the envelope, public access, the required+validated `company_id`,
the per-company timezone lookup, and the rate limit.
design/registration/server-time-api.md.
"""

import datetime

import pytest
from django.core.cache import cache

from apps.company.models import Company, Country, State
from apps.portal import api

URL = "/api/v1/operator/server/time"


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


def ask(client, company_id="BYKY", **rest):
    return call(client, {"company_id": company_id, **rest})


def assert_envelope(response):
    body = response.json()
    assert set(body) == {"code", "message", "data"}
    return body


@pytest.fixture
def company(db):
    country = Country.objects.create(short_code="AE", name="United Arab Emirates")
    state = State.objects.create(country=country, short_code="AUH", name="Abu Dhabi")
    return Company.objects.create(
        short_code="BYKY", name="BYKY", country=country, state=state,
        phone_number="+9710000000", email="ops@byky.test",
    )


# -- The foundation every device/portal API shares ----------------------------


def test_no_token_is_needed(client, company):
    response = ask(client)

    assert response.status_code == 200
    assert assert_envelope(response)["code"] == "ok"


@pytest.mark.parametrize("app", ["operator", "manager", "employee"])
def test_each_phone_app_has_the_endpoint(client, company, app):
    response = call(client, {"company_id": "BYKY"}, url=f"/api/v1/{app}/server/time")
    assert response.status_code == 200


def test_a_get_is_405_in_the_envelope(client):
    response = client.get(URL)

    assert response.status_code == 405
    assert assert_envelope(response)["code"] == "method_not_allowed"


def test_a_body_that_is_not_json_is_400_in_the_envelope(client):
    response = client.post(URL, "{not json", content_type="application/json")

    assert response.status_code == 400
    assert assert_envelope(response)["code"] == "invalid_request"


def test_a_crash_still_answers_in_the_envelope(client, company, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("bug")
    monkeypatch.setattr(api, "_server_time_payload", boom)

    response = ask(client)

    assert response.status_code == 500
    body = assert_envelope(response)
    assert body["code"] == "server_error"
    assert "bug" not in body["message"]


def test_the_credentials_block_is_accepted_whatever_it_holds(client, company):
    credentials = {
        "device_id": "", "token_id": None, "imei": ["x"],
        "latitude": "not a number", "send_status": {"nested": True},
    }
    response = ask(client, credentials=credentials)

    assert response.status_code == 200


def test_a_malformed_credentials_block_is_ignored(client, company):
    response = call(client, body={"credentials": "junk", "request_data": {"company_id": "BYKY"}})

    assert response.status_code == 200


def test_the_body_must_be_an_object(client):
    response = call(client, body=[1, 2])

    assert response.status_code == 400
    assert assert_envelope(response)["code"] == "invalid_request"


def test_request_data_must_be_an_object(client):
    response = call(client, body={"request_data": ["x"]})

    assert response.status_code == 400
    assert assert_envelope(response)["code"] == "invalid_request"


def test_unknown_request_keys_are_ignored(client, company):
    response = ask(client, version_code=1, hierarchy_id=12, username="sara")

    assert response.status_code == 200


# -- company_id: required and validated ---------------------------------------


def test_company_id_is_required(client, company):
    response = call(client, {})

    assert response.status_code == 400
    body = assert_envelope(response)
    assert body["code"] == "invalid_request"
    assert body["data"]["errors"] == {"company_id": "is required"}


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_company_id_is_required(client, company, blank):
    response = call(client, {"company_id": blank})

    assert response.status_code == 400
    assert assert_envelope(response)["data"]["errors"] == {"company_id": "is required"}


def test_a_non_text_company_id_is_treated_as_missing(client, company):
    """Coerced to empty before validation, same as every other field this
    project reads from request_data or credentials -- a wrong type is never
    trusted just because it is truthy."""
    response = call(client, {"company_id": 123})

    assert response.status_code == 400
    assert assert_envelope(response)["data"]["errors"] == {"company_id": "is required"}


def test_an_oversized_company_id_is_refused(client, company):
    response = call(client, {"company_id": "x" * 11})

    assert response.status_code == 400
    assert "company_id" in assert_envelope(response)["data"]["errors"]


def test_an_unknown_company_id_is_refused_not_defaulted(client, company):
    """No guessing: an unmatched code is `unknown_company`, never a silently
    swapped-in timezone."""
    response = ask(client, company_id="NOPE")

    assert response.status_code == 400
    body = assert_envelope(response)
    assert body["code"] == "unknown_company"
    assert body["data"] == {}


def test_company_id_may_also_arrive_in_credentials(client, company):
    company.timezone = "Asia/Kuwait"
    company.save(update_fields=["timezone"])

    body = assert_envelope(call(client, {}, credentials={"company_id": "BYKY"}))["data"]

    assert body["company_timezone"] == "Asia/Kuwait"


def test_request_data_company_id_wins_over_credentials(client, company):
    second = Company.objects.create(
        short_code="1001", name="Second", country=company.country, state=company.state,
        phone_number="+9712222222", email="two@byky.test", timezone="Asia/Kuwait",
    )

    body = assert_envelope(
        call(client, {"company_id": "1001"}, credentials={"company_id": "BYKY"})
    )["data"]

    assert body["company_timezone"] == "Asia/Kuwait"


# -- The payload ---------------------------------------------------------------


def test_the_response_shape(client, company):
    body = assert_envelope(ask(client))

    assert body["code"] == "ok"
    assert set(body["data"]) == {"server_utc_date_time", "company_timezone", "utc_offset"}
    assert body["data"]["server_utc_date_time"].endswith("Z")


def test_the_matched_companys_own_timezone_is_returned(client, company):
    company.timezone = "Asia/Kuwait"
    company.save(update_fields=["timezone"])

    body = assert_envelope(ask(client, company_id="BYKY"))["data"]

    assert body["company_timezone"] == "Asia/Kuwait"
    assert body["utc_offset"] == "+03:00"


def test_each_company_gets_its_own_zone_even_when_several_share_the_database(client, company):
    second = Company.objects.create(
        short_code="1001", name="Second", country=company.country, state=company.state,
        phone_number="+9712222222", email="two@byky.test", timezone="Asia/Kuwait",
    )

    dubai = assert_envelope(ask(client, company_id="BYKY"))["data"]
    kuwait = assert_envelope(ask(client, company_id="1001"))["data"]

    assert dubai["company_timezone"] == "Asia/Dubai"
    assert kuwait["company_timezone"] == "Asia/Kuwait"


def test_an_inactive_company_is_treated_as_unknown(client, company):
    company.is_active = False
    company.save(update_fields=["is_active"])

    response = ask(client, company_id="BYKY")

    assert response.status_code == 400
    assert assert_envelope(response)["code"] == "unknown_company"


def test_server_utc_date_time_is_real_utc(client, company, monkeypatch):
    pinned = datetime.datetime(2026, 9, 22, 10, 0, tzinfo=datetime.UTC)
    monkeypatch.setattr(api.timezone, "now", lambda: pinned)

    body = assert_envelope(ask(client))["data"]

    assert body["server_utc_date_time"] == "2026-09-22T10:00:00Z"


def test_the_call_changes_nothing(client, company, django_assert_max_num_queries):
    with django_assert_max_num_queries(2) as queries:
        ask(client)

    sql = " ".join(q["sql"].upper() for q in queries.captured_queries)
    assert "INSERT" not in sql and "UPDATE " not in sql and "DELETE" not in sql


# -- Rate limit ------------------------------------------------------------


def test_too_many_calls_are_429_in_the_envelope(client, company, monkeypatch):
    monkeypatch.setattr(api.ServerTimeIPThrottle, "rate", "2/minute", raising=False)

    ask(client)
    ask(client)
    response = ask(client)

    assert response.status_code == 429
    assert assert_envelope(response)["code"] == "rate_limited"
    assert int(response["Retry-After"]) > 0

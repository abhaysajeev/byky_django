"""POST /api/v1/{app}/device/registration, through HTTP as the tablet sees it.

design/03-login.md section 9B; the contract handed to the app team is
design/registration/registration-api.md. One tablet at a time, through every
state the handout lists.
"""

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.company.models import Company
from apps.devices import api, services
from apps.devices.models import Device, DeviceStatus
from apps.fare.tests import test_api as fare_api
from core.enums import Channel

OPERATOR = Channel.OPERATOR

# A signed-up operator with a mapped tablet, from the fare API tests.
world = fare_api.world


@pytest.fixture(autouse=True)
def fresh_throttle():
    cache.clear()
    yield
    cache.clear()


def register(client, installation_id="install-1", *, platform="android", platform_id="fed25",
             model="Samsung SM-A556E", credentials=None, app="operator", **extra):
    request_data = {"installation_id": installation_id, "platform": platform,
                    "platform_id": platform_id, "device_model": model, **extra}
    return client.post(
        f"/api/v1/{app}/device/registration",
        {"credentials": credentials or {}, "request_data": request_data},
        content_type="application/json",
    )


def body(response):
    data = response.json()
    assert set(data) == {"code", "message", "data"}
    return data


def approved_device(company, installation_id="install-1", *, platform_id="fed25", name="AlMamzar-POS1",
                    channel=OPERATOR, status=DeviceStatus.APPROVED):
    return Device.objects.create(
        company=company, installation_id=installation_id, platform="android",
        platform_id=platform_id, channel=channel, status=status, name=name,
        approved_at=timezone.now() if status != DeviceStatus.PENDING else None,
        last_seen_at=timezone.now(),
    )


# -- A new tablet ---------------------------------------------------------------


def test_a_new_tablet_is_numbered_and_waits(client, company):
    response = register(client)

    assert response.status_code == 202
    reply = body(response)
    assert reply["code"] == "pending_approval"
    device = Device.objects.get(installation_id="install-1")
    assert reply["data"]["device_registration_id"] == device.device_registration_id
    assert reply["data"]["registered_at"].endswith("Z")
    assert device.status == DeviceStatus.PENDING
    assert device.company == company                 # the deployment's, never the app's
    assert (device.platform_id, device.device_model, device.channel) == ("fed25", "Samsung SM-A556E", "operator")


def test_no_token_is_needed(client, company):
    assert register(client).status_code == 202


def test_calling_again_changes_nothing(client, company):
    first = body(register(client))["data"]["device_registration_id"]
    second = body(register(client))["data"]["device_registration_id"]

    assert first == second
    assert Device.objects.count() == 1


def test_racing_first_calls_make_one_row(company, monkeypatch):
    """Two first calls at once: both look, neither finds a row, both insert.
    The loser hits the unique index and answers from the winner's row."""
    winner = Device.objects.create(
        company=company, installation_id="race", platform="android", channel=OPERATOR,
    )
    monkeypatch.setattr(services, "device_for_installation", lambda installation_id: None)

    outcome, device = services.register_device(installation_id="race", channel=OPERATOR, platform="android")

    assert outcome == services.PENDING
    assert device.pk == winner.pk
    assert Device.objects.filter(installation_id="race").count() == 1


def test_the_app_names_its_company(client, company):
    """One server, several companies: before approval a tablet has no user, no
    station and nothing else that could say where it belongs, so it says so
    itself. Matched on Company.short_code and taken as sent -- approval is the
    control (design/03-login.md section 9B.2)."""
    second = Company.objects.create(
        short_code="1001", name="Second", country=company.country, state=company.state,
        phone_number="+9712222222", email="two@byky.test",
    )

    assert register(client, company_id="1001").status_code == 202

    assert Device.objects.get().company == second


def test_a_numeric_company_code_is_matched_as_text(client, company):
    """The client's licence server issues numeric company ids, which live in
    the text short_code column."""
    company.short_code = "7"
    company.save(update_fields=["short_code"])

    assert register(client, company_id=7).status_code == 202
    assert Device.objects.get().company == company


def test_the_company_may_also_arrive_in_the_credentials_block(client, company):
    """The app team's standard block already carries CompanyID."""
    company.short_code = "42"
    company.save(update_fields=["short_code"])

    assert register(client, credentials={"company_id": "42"}).status_code == 202
    assert Device.objects.get().company == company


def test_a_company_nobody_has_set_up_is_refused(client, company):
    response = register(client, company_id="nope")

    assert response.status_code == 400
    assert body(response)["code"] == "unknown_company"
    assert Device.objects.count() == 0


def test_a_known_tablet_is_never_re_homed(client, company):
    """A device belongs to the company that approved it. A later call naming
    another one answers from the row it already has."""
    register(client)
    Company.objects.create(
        short_code="1001", name="Second", country=company.country, state=company.state,
        phone_number="+9712222222", email="two@byky.test",
    )

    assert register(client, company_id="1001").status_code == 202
    assert Device.objects.get().company == company


def test_with_several_companies_and_no_code_registration_is_unavailable(client, company):
    Company.objects.create(
        short_code="TWO", name="Second", country=company.country, state=company.state,
        phone_number="+9712222222", email="two@byky.test",
    )
    response = register(client)

    assert response.status_code == 503
    assert body(response)["code"] == "registration_unavailable"
    assert Device.objects.count() == 0


# -- Known tablets ---------------------------------------------------------------


def test_an_approved_tablet_gets_its_name_and_no_branch(client, company, branch):
    device = approved_device(company)

    response = register(client)

    assert response.status_code == 200
    reply = body(response)
    assert reply["code"] == "approved"
    assert reply["data"] == {
        "device_registration_id": device.device_registration_id,
        "name": "AlMamzar-POS1",
        "approved_at": reply["data"]["approved_at"],
    }
    assert reply["data"]["approved_at"].endswith("Z")


@pytest.mark.parametrize("status,code", [
    (DeviceStatus.BLOCKED, "device_blocked"),
    (DeviceStatus.RETIRED, "device_retired"),
])
def test_a_blocked_or_retired_tablet_is_stopped(client, company, status, code):
    approved_device(company, status=status)

    response = register(client)

    assert response.status_code == 403
    assert body(response) == {"code": code, "message": body(response)["message"], "data": {}}


@pytest.mark.parametrize("status,http,code", [
    (DeviceStatus.APPROVED, 200, "approved"),
    (DeviceStatus.PENDING, 202, "pending_approval"),
    (DeviceStatus.BLOCKED, 403, "device_blocked"),
    (DeviceStatus.RETIRED, 403, "device_retired"),
])
def test_every_app_on_a_phone_shares_its_one_registration(client, company, status, http, code):
    """The operator, manager and employee apps send one installation_id: the
    phone is registered and approved once, and every app is told its state."""
    device = approved_device(company, channel=Channel.EMPLOYEE, status=status)

    for app in ("operator", "manager", "employee"):
        response = register(client, app=app)
        assert (response.status_code, body(response)["code"]) == (http, code), app
        if code in ("approved", "pending_approval"):
            assert body(response)["data"]["device_registration_id"] == device.device_registration_id
    assert Device.objects.count() == 1
    device.refresh_from_db()
    assert device.channel == Channel.EMPLOYEE              # still the app that registered it first


def test_every_call_records_when_the_tablet_was_seen(client, company):
    device = approved_device(company)
    device.last_seen_at = None
    device.save()

    register(client)

    device.refresh_from_db()
    assert device.last_seen_at is not None


def test_the_push_token_is_saved_and_updated(client, company):
    register(client, credentials={"device_notification_id": "fcm-1"})
    assert Device.objects.get().push_token == "fcm-1"

    register(client, credentials={"device_notification_id": "fcm-2"})
    assert Device.objects.get().push_token == "fcm-2"


@pytest.mark.parametrize("junk", [None, "", 12, ["x"], {"a": 1}, "x" * 513])
def test_an_odd_push_token_is_ignored_not_an_error(client, company, junk):
    register(client, credentials={"device_notification_id": "fcm-1"})

    response = register(client, credentials={"device_notification_id": junk})

    assert response.status_code == 202
    assert Device.objects.get().push_token == "fcm-1"


def test_a_resigned_app_updates_the_platform_id_of_an_approved_tablet(client, company):
    approved_device(company, platform_id="old-id")

    assert register(client, platform_id="new-id").status_code == 200
    assert Device.objects.get().platform_id == "new-id"


def test_a_pending_tablet_cannot_rewrite_its_platform_id(client, company):
    register(client, platform_id="first")
    register(client, platform_id="second")

    assert Device.objects.get().platform_id == "first"


# -- Reinstall, factory reset ---------------------------------------------------


def test_a_reinstall_waits_on_the_same_row_and_the_original_keeps_working(client, company):
    original = approved_device(company, "old-install", platform_id="fed25")
    number = original.device_registration_id

    response = register(client, "new-install", platform_id="fed25", model="Redmi Note 9")

    assert response.status_code == 202
    reply = body(response)
    assert reply["code"] == "reconnect_pending"
    assert reply["data"]["device_registration_id"] == number
    assert reply["data"]["name"] == "AlMamzar-POS1"
    assert reply["data"]["reinstall_requested_at"]
    assert Device.objects.count() == 1
    original.refresh_from_db()
    assert (original.installation_id, original.pending_installation_id, original.pending_device_model) == (
        "old-install", "new-install", "Redmi Note 9")
    assert body(register(client, "old-install"))["code"] == "approved"


def test_asking_again_after_a_reinstall_gives_the_same_answer(client, company):
    approved_device(company, "old-install", platform_id="fed25")
    first = body(register(client, "new-install", platform_id="fed25"))["data"]["device_registration_id"]
    second = body(register(client, "new-install", platform_id="fed25"))

    assert second["code"] == "reconnect_pending"
    assert second["data"]["device_registration_id"] == first
    assert Device.objects.count() == 1


def test_a_second_reinstall_replaces_the_one_waiting(client, company):
    original = approved_device(company, "old-install", platform_id="fed25")
    register(client, "install-2", platform_id="fed25")

    register(client, "install-3", platform_id="fed25")

    original.refresh_from_db()
    assert original.pending_installation_id == "install-3"
    assert Device.objects.count() == 1


def test_a_pending_device_simply_follows_its_phone(client, company):
    first = body(register(client, "install-1", platform_id="fed25"))["data"]["device_registration_id"]

    again = body(register(client, "install-2", platform_id="fed25"))

    assert (again["code"], again["data"]["device_registration_id"]) == ("pending_approval", first)
    assert list(Device.objects.values_list("installation_id", flat=True)) == ["install-2"]


def test_a_blocked_device_stays_blocked_when_reinstalled(client, company):
    original = approved_device(company, "old-install", platform_id="fed25", status=DeviceStatus.BLOCKED)

    assert body(register(client, "new-install", platform_id="fed25"))["code"] == "device_blocked"
    original.refresh_from_db()
    assert original.pending_installation_id == "new-install"


@pytest.mark.parametrize("setup", ["retired", "other_app", "other_platform", "other_phone"])
def test_what_never_counts_as_a_reinstall(client, company, setup):
    if setup == "retired":
        approved_device(company, "old", platform_id="fed25", status=DeviceStatus.RETIRED)
    elif setup == "other_app":
        approved_device(company, "old", platform_id="fed25", channel=Channel.MANAGER)
    else:
        approved_device(company, "old", platform_id="fed25")

    kwargs = {"platform_id": "fed25"}
    if setup == "other_platform":
        kwargs["platform"] = "ios"
    if setup == "other_phone":
        kwargs["platform_id"] = "other"

    reply = body(register(client, "new-install", **kwargs))

    assert reply["code"] == "pending_approval"
    assert Device.objects.filter(installation_id="new-install").exists()


def test_the_platform_id_is_required(client, company):
    reply = body(register(client, "new-install", platform_id=""))

    assert reply["code"] == "invalid_request" and "platform_id" in reply["data"]["errors"]


def test_a_factory_reset_is_a_new_device(client, company):
    approved_device(company, "old-install", platform_id="fed25")

    reply = body(register(client, "reset-install", platform_id="brand-new"))

    assert reply["code"] == "pending_approval"


# -- Bad requests, limits ---------------------------------------------------------


@pytest.mark.parametrize("field,value", [
    ("installation_id", ""),
    ("installation_id", "x" * 65),
    ("platform", "windows"),
    ("platform", ""),
    ("platform_id", "x" * 65),
    ("device_model", "x" * 121),
])
def test_a_bad_field_is_named(client, company, field, value):
    response = register(client, **{field: value}) if field != "installation_id" else register(client, value)

    assert response.status_code == 400
    reply = body(response)
    assert reply["code"] == "invalid_request"
    assert field in reply["data"]["errors"]
    assert Device.objects.count() == 0


def test_the_web_has_no_registration(client, company):
    response = register(client, app="web")
    assert response.status_code == 404


def test_one_installation_is_rate_limited(client, company, monkeypatch):
    monkeypatch.setattr(api.RegistrationInstallationThrottle, "rate", "2/minute", raising=False)

    register(client)
    register(client)
    response = register(client)

    assert response.status_code == 429
    assert body(response)["code"] == "rate_limited"


def test_one_address_is_rate_limited(client, company, monkeypatch):
    monkeypatch.setattr(api.RegistrationIPThrottle, "rate", "2/minute", raising=False)

    register(client, "a")
    register(client, "b")
    response = register(client, "c")

    assert response.status_code == 429


def test_the_operator_app_signs_in_on_a_phone_the_employee_app_registered(client, world):
    """Login finds the phone by installation_id alone; the operator app still
    needs what it always needs -- approved, and mapped to a station."""
    Device.objects.filter(installation_id="till-1").update(channel=Channel.EMPLOYEE)

    response = fare_api.call(client, "/api/v1/operator/auth/login",
                             {"username": "OPR001", "password": fare_api.PASSWORD, "installation_id": "till-1"})

    assert (response.status_code, response.json()["code"]) == (200, "ok")

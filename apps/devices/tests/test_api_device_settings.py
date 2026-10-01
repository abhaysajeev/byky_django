"""POST /api/v1/operator/device/settings, through HTTP: the login's settings
block on its own -- station settings, receipt prefix and counters -- for a
signed-in operator tablet."""

import datetime

import pytest
from django.utils import timezone

from apps.devices import api
from apps.devices.models import BillContinuity, DeviceSettings
from apps.portal.tests import test_operator_login_api as login_api
from core.timezones import zone_for

# The signed-in till from the operator login tests, reused as fixtures here.
call, _login = login_api.call, login_api._login
world, employee, app_user, device = login_api.world, login_api.employee, login_api.app_user, login_api.device
fresh_throttle = login_api.fresh_throttle

URL = "/api/v1/operator/device/settings"


@pytest.fixture
def token(client, app_user, device):
    response = _login(client, app_user, device)
    assert response.status_code == 200, response.json()
    return response.json()["data"]["tokens"]["access"]


def settings(client, token, request_data=None, app="operator"):
    return call(client, URL.replace("operator", app), request_data,
                headers={"HTTP_AUTHORIZATION": f"Bearer {token}"}).json()


def test_it_returns_the_same_block_as_login(client, app_user, device, world):
    login = _login(client, app_user, device).json()["data"]

    body = settings(client, login["tokens"]["access"])

    assert body["code"] == "ok"
    assert body["data"] == {key: login[key] for key in (
        "device_settings", "order_no_prefix", "last_order_no", "next_order_number", "next_test_number")}
    # The Swagger example shortens device_settings, as login's does; the rest is exact.
    assert set(body["data"]) == set(api._SETTINGS_SAMPLE)
    assert set(api._SETTINGS_SAMPLE["device_settings"]) <= set(body["data"]["device_settings"])


def test_it_tells_the_last_receipt_the_server_has_seen(client, token, device, world):
    BillContinuity.objects.filter(device=device, kind="order").update(last_number=334)

    data = settings(client, token)["data"]

    assert (data["last_order_no"], data["next_order_number"]) == (
        f"CREEK{device.device_registration_id}000334", 335)


def test_the_logo_is_sent_again_only_if_it_changed_since(client, token, world):
    changed = timezone.now()
    DeviceSettings.objects.filter(branch=world["branch"]).update(logo=b"\x89PNG-bitmap", logo_changed_on=changed)
    zone = zone_for(world["company"])
    before = (changed - datetime.timedelta(hours=1)).astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")
    after = (changed + datetime.timedelta(hours=1)).astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")

    assert settings(client, token)["data"]["device_settings"]["logo"]
    assert settings(client, token, {"since": before})["data"]["device_settings"]["logo"]
    assert settings(client, token, {"since": after})["data"]["device_settings"]["logo"] == ""


def test_since_is_checked(client, token):
    body = settings(client, token, {"since": "yesterday"})

    assert body["code"] == "invalid_request" and "since" in body["data"]["errors"]


def test_a_station_whose_settings_were_withdrawn_is_told_so(client, token, world):
    DeviceSettings.objects.filter(branch=world["branch"]).update(is_active=False)

    body = settings(client, token)

    assert body["code"] == "device_settings_not_done"


def test_a_closed_station_is_refused(client, token, world):
    world["branch"].is_active = False
    world["branch"].save()

    assert settings(client, token)["code"] == "branch_inactive"


def test_it_is_for_the_operator_app_only(client, token):
    assert settings(client, token, app="manager")["code"] == "wrong_channel"


def test_signed_out_is_refused(client, world):
    assert call(client, URL).status_code == 401


def test_the_endpoint_is_in_the_api_docs(client, db):
    assert "/api/v1/{app}/device/settings" in client.get("/api/schema/").content.decode()

"""POST /api/v1/operator/device/config (owner, 5 Nov 2026): this tablet's
Branch-Device Config -- For Rent / For Receive, advisory."""

import pytest

from apps.devices.models import Device
from apps.portal.session_models import AppSession
from apps.portal.tests import test_operator_login_api as login_api

call, _login = login_api.call, login_api._login
world, employee, app_user, device = login_api.world, login_api.employee, login_api.app_user, login_api.device
fresh_throttle = login_api.fresh_throttle

URL = "/api/v1/operator/device/config"


@pytest.fixture
def token(client, app_user, device):
    response = _login(client, app_user, device)
    assert response.status_code == 200, response.json()
    return response.json()["data"]["tokens"]["access"]


def config(client, token, app="operator"):
    return call(client, URL.replace("operator", app), None, headers={"HTTP_AUTHORIZATION": f"Bearer {token}"})


def test_it_returns_this_tablets_flags_and_branch(client, token, device, world):
    Device.objects.filter(pk=device.pk).update(for_rent=False)

    body = config(client, token).json()

    assert body["code"] == "ok", body
    data = body["data"]
    assert data["config"] == {"for_rent": False, "for_receive": True}
    assert data["device_registration_id"] == Device.objects.get(pk=device.pk).device_registration_id
    assert data["branch"]["branch_id"] == world["branch"].pk


def test_both_are_on_by_default(client, token):
    assert config(client, token).json()["data"]["config"] == {"for_rent": True, "for_receive": True}


def test_another_apps_url_is_refused(client, token):
    response = config(client, token, app="manager")
    assert (response.status_code, response.json()["code"]) == (403, "wrong_channel")


def test_a_session_without_a_branch_is_refused(client, token, device):
    AppSession.objects.filter(device=device, logged_out_at__isnull=True).update(branch=None)

    response = config(client, token)

    assert (response.status_code, response.json()["code"]) == (409, "device_not_mapped")


def test_signed_out_is_refused(client, db):
    assert call(client, URL, None).status_code == 401

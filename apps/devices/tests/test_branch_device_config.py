"""Branch-Device Config (owner, 5 Nov 2026): at a multi-device branch, For Rent /
For Receive per operator tablet. The branch only filters; the flags are the
device's own and advisory (device/config hands them to the app)."""

import importlib
import json

import pytest
from django.apps import apps as django_apps
from django.utils import timezone

from apps.company.models import Branch, Company
from apps.devices.models import Device, DeviceMapping, DeviceStatus
from apps.portal.models import Page, Role, RolePermission
from core.enums import Channel

URL = "/devices/branch-device-config/"


def save_url(device):
    return f"{URL}{device.pk}/save/"


def post(client, url, payload):
    return client.post(url, json.dumps(payload), content_type="application/json")


def tablet(company, installation_id, channel=Channel.OPERATOR, name=None):
    return Device.objects.create(
        company=company, installation_id=installation_id, platform="android", channel=channel,
        status=DeviceStatus.APPROVED, approved_at=timezone.now(), name=name or f"POS-{installation_id}",
    )


def mapped(device, branch, *, closed=False, letter=""):
    now = timezone.now()
    return DeviceMapping.objects.create(device=device, branch=branch, from_date=now,
                                        to_date=now if closed else None, transaction_letter=letter)


@pytest.fixture
def multi(branch):
    Branch.objects.filter(pk=branch.pk).update(is_multi_device=True)
    branch.refresh_from_db()
    return branch


def test_a_new_device_is_set_for_both(company):
    device = tablet(company, "t-new")
    assert (device.for_rent, device.for_receive) == (True, True)


def test_the_page_opens_for_a_role_with_it(signed_in, multi):
    assert signed_in.get(URL).status_code == 200


def test_the_page_is_refused_without_it(no_permissions, multi):
    assert no_permissions.get(URL).status_code == 403


def test_only_multi_device_branches_are_offered(signed_in, multi, other_branch):
    body = signed_in.get(URL).content.decode()

    assert f'<option value="{multi.pk}"' in body
    assert f'<option value="{other_branch.pk}"' not in body          # single-device branch


def test_no_multi_device_branch_says_so(signed_in, branch):
    body = signed_in.get(URL).content.decode()
    assert "No multi-device branches" in body and "config-branch-select" not in body


def test_it_lists_only_this_branchs_mapped_operator_tablets(signed_in, company, multi, other_branch):
    Branch.objects.filter(pk=other_branch.pk).update(is_multi_device=True)
    here = tablet(company, "t-here", name="Here-POS1")
    mapped(here, multi, letter="A")
    mapped(tablet(company, "t-away", name="Away-POS1"), other_branch)
    mapped(tablet(company, "t-closed", name="Closed-POS1"), multi, closed=True)
    mapped(tablet(company, "p-emp", channel=Channel.EMPLOYEE, name="Emp-Phone"), multi)

    page = signed_in.get(f"{URL}?branch={multi.pk}")

    names = [d["name"] for d in page.context["devices"]]
    assert names == ["Here-POS1"]
    assert page.context["devices"][0]["transaction_letter"] == "A"
    assert page.context["devices"][0]["flags"] == [("for_rent", True), ("for_receive", True)]


def test_saving_sets_each_flag_and_any_combination(signed_in, company, multi):
    device = tablet(company, "t-1")
    mapped(device, multi)

    assert post(signed_in, save_url(device), {"for_rent": False}).json()["ok"] is True
    device.refresh_from_db()
    assert (device.for_rent, device.for_receive) == (False, True)

    assert post(signed_in, save_url(device), {"for_receive": False}).json()["ok"] is True
    device.refresh_from_db()
    assert (device.for_rent, device.for_receive) == (False, False)        # no rule on combinations

    post(signed_in, save_url(device), {"for_rent": True, "for_receive": True})
    device.refresh_from_db()
    assert (device.for_rent, device.for_receive) == (True, True)


def test_saving_needs_update(client, company, multi):
    from django.test import Client

    from apps.portal.services import grant_all
    from core.models import User

    role = Role.objects.create(company=company, name="Viewer")
    grant_all(role)
    RolePermission.objects.filter(role=role, page__code="devices.branch_device_config").update(can_update=False)
    User.objects.create_user("viewer.b", "Byky#2026", company=company, role=role, allowed_channels=[Channel.WEB])
    viewer = Client()
    viewer.post("/login/", {"username": "viewer.b", "password": "Byky#2026"})
    device = tablet(company, "t-2")
    mapped(device, multi)

    assert viewer.get(URL).status_code == 200
    assert post(viewer, save_url(device), {"for_rent": False}).status_code == 403
    device.refresh_from_db()
    assert device.for_rent is True


def test_another_companys_or_a_non_operator_device_is_refused(signed_in, company, multi):
    other = Company.objects.create(short_code="OTH", name="Other", country=company.country, state=company.state,
                                   phone_number="+9710000001", email="ops@other.test")
    theirs = tablet(other, "t-theirs")
    phone = tablet(company, "p-mgr", channel=Channel.MANAGER)

    for device in (theirs, phone):
        response = post(signed_in, save_url(device), {"for_rent": False})
        assert response.status_code == 400 and response.json()["ok"] is False
        device.refresh_from_db()
        assert device.for_rent is True


def test_the_migration_grants_the_page_to_roles_that_change_device_mapping(company):
    mapper = Role.objects.create(company=company, name="Device admin")
    reader = Role.objects.create(company=company, name="Reader")
    RolePermission.objects.create(role=mapper, page=Page.objects.get(code="devices.device_mapping"),
                                  can_read=True, can_update=True)
    RolePermission.objects.create(role=reader, page=Page.objects.get(code="devices.device_mapping"), can_read=True)

    migration = importlib.import_module("apps.portal.migrations.0033_branch_device_config")
    migration.apply_registry(django_apps, None)

    row = RolePermission.objects.get(role=mapper, page__code="devices.branch_device_config")
    assert (row.can_read, row.can_update) == (True, True)
    assert not RolePermission.objects.filter(role=reader, page__code="devices.branch_device_config").exists()

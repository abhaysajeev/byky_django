"""Device Mapping's transaction letter (owner, 28 Oct 2026): at a multi-device
station each tablet carries a letter -- DUP-A-1, DUP-B-1 -- set on its
mapping; the prefix is always the station's current order prefix."""

import pytest
from django.utils import timezone

from apps.devices import services
from apps.devices.models import DeviceMapping, DeviceSettings
from apps.devices.tests import test_device_mapping_actions as actions
from core.enums import ApprovalStatus

admin, devices, tablet, mapped, post = actions.admin, actions.devices, actions.tablet, actions.mapped, actions.post


@pytest.fixture
def multi(branch):
    branch.is_multi_device = True
    branch.save()
    return branch


def test_a_letter_on_map_upper_cased_and_unique_at_the_station(devices, admin, company, multi):
    first, second = tablet(company, "tab-1"), tablet(company, "tab-2")
    assert services.map_device(devices, first.pk, user=admin, branch=multi, letter="a").transaction_letter == "A"
    with pytest.raises(services.DeviceActionError, match="Letter A is already used"):
        services.map_device(devices, second.pk, user=admin, branch=multi, letter="A")
    assert services.map_device(devices, second.pk, user=admin, branch=multi, letter="B").transaction_letter == "B"


@pytest.mark.parametrize("letter", ["AB", "1", "-"])
def test_only_one_letter_a_to_z(devices, admin, company, multi, letter):
    with pytest.raises(services.DeviceActionError, match="one letter, A to Z"):
        services.map_device(devices, tablet(company).pk, user=admin, branch=multi, letter=letter)


def test_no_letter_at_a_single_device_station(devices, admin, company, branch):
    assert services.map_device(devices, tablet(company).pk, user=admin, branch=branch, letter="A").transaction_letter \
        == ""


def test_a_letter_is_free_again_once_its_mapping_is_closed(devices, admin, company, multi):
    first, second = tablet(company, "tab-1"), tablet(company, "tab-2")
    services.map_device(devices, first.pk, user=admin, branch=multi, letter="A")
    services.close_mapping(devices, first.pk, user=admin)
    assert services.map_device(devices, second.pk, user=admin, branch=multi, letter="A").transaction_letter == "A"


def test_remap_and_set_letter(devices, admin, company, branch, other_branch):
    other_branch.is_multi_device = True
    other_branch.save()
    device = tablet(company)
    mapped(device, branch)
    services.remap_device(devices, device.pk, user=admin, branch=other_branch, letter="c")
    assert DeviceMapping.objects.get(device=device, to_date__isnull=True).transaction_letter == "C"
    services.set_transaction_letter(devices, device.pk, user=admin, letter="d")
    rows = DeviceMapping.objects.filter(device=device)
    assert rows.count() == 2 and rows.get(to_date__isnull=True).transaction_letter == "D"   # in place
    services.set_transaction_letter(devices, device.pk, user=admin, letter="")
    assert rows.get(to_date__isnull=True).transaction_letter == ""


def test_set_letter_needs_a_multi_device_station(devices, admin, company, branch):
    device = tablet(company)
    mapped(device, branch)
    with pytest.raises(services.DeviceActionError, match="not a multi-device station"):
        services.set_transaction_letter(devices, device.pk, user=admin, letter="A")


def test_the_prefix_is_the_current_order_prefix_and_the_letter(company, multi):
    DeviceSettings.objects.update_or_create(branch=multi, defaults={
        "settings_code": "S-T", "order_no_prefix": "DUP", "approval_status": ApprovalStatus.APPROVED})
    device = tablet(company)
    mapping = DeviceMapping.objects.create(device=device, branch=multi, from_date=timezone.now(),
                                           transaction_letter="A")
    assert services.transaction_no_prefix(multi, mapping) == "DUP-A"
    DeviceSettings.objects.filter(branch=multi).update(order_no_prefix="DUB")
    assert services.transaction_no_prefix(multi, mapping) == "DUB-A"            # the current prefix
    multi.is_multi_device = False
    assert services.transaction_no_prefix(multi, mapping) == "DUB"


# -- Screen and endpoints -----------------------------------------------------------------


def test_the_screen_and_its_endpoints(signed_in, company, multi):
    device = tablet(company)
    assert post(signed_in, "/devices/mapping/save/", {
        "device": device.pk, "branch": multi.pk, "transaction_letter": "a"}).json()["ok"] is True
    assert DeviceMapping.objects.get(device=device).transaction_letter == "A"
    assert post(signed_in, f"/devices/mapping/{device.pk}/letter/", {"transaction_letter": "z"}).json()["ok"] is True
    assert DeviceMapping.objects.get(device=device).transaction_letter == "Z"
    page = signed_in.get("/devices/mapping/")
    row = next(d for d in page.context["devices"] if d["pk"] == device.pk)
    assert row["transaction_prefix"].endswith("-Z") and row["multi_device"] is True
    flag = next(b for b in page.context["branch_flags"] if b["id"] == multi.pk)
    assert flag["multi_device"] is True and flag["prefix"] == services.bill_prefix_for(multi)
    bad = post(signed_in, f"/devices/mapping/{device.pk}/letter/", {"transaction_letter": "9"}).json()
    assert bad["ok"] is False

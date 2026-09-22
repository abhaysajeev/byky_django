"""The Device Approval actions: approve, replace, reconnect, reject, block,
unblock -- and the whole loop with the registration API.

design/03-login.md sections 9B.3-9B.5. Every action re-checks the device's
status under a row lock, so the "already done" cases matter as much as the
happy ones.
"""

import json

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.company.models import Branch, BranchType, Company
from apps.devices import scoping, services
from apps.devices.models import (
    Device,
    DeviceAction,
    DeviceMapping,
    DeviceStatus,
    DeviceStatusLog,
)
from apps.portal.auth import open_session
from apps.portal.session_models import LogoutReason
from core.enums import Channel, UserScope
from core.models import User

OPERATOR = Channel.OPERATOR


@pytest.fixture(autouse=True)
def fresh_throttle():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def admin(company):
    return User.objects.create_user(
        "admin.a", "Byky#2026", company=company, scope=UserScope.COMPANY,
        allowed_channels=[Channel.WEB],
    )


@pytest.fixture
def devices(admin):
    return scoping.devices_for(admin)


def pending(company, installation_id="install-1", *, channel=OPERATOR, platform_id="fed25", reconnect_of=None):
    return Device.objects.create(
        company=company, installation_id=installation_id, platform="android",
        platform_id=platform_id, channel=channel, status=DeviceStatus.PENDING,
        reconnect_of=reconnect_of,
    )


def approve(devices, device, admin, branch=None, name="AlMamzar-POS1", **kw):
    return services.approve_device(devices, device.pk, user=admin, name=name, branch=branch, **kw)


def live_session(device, branch):
    operator = User.objects.create_user(
        f"op.{device.pk}", "Byky#2026", company=device.company, scope=UserScope.COMPANY,
        allowed_channels=[OPERATOR],
    )
    return open_session(operator, OPERATOR, device=device, branch=branch)


def actions(device):
    return list(DeviceStatusLog.objects.filter(device=device).values_list("action", flat=True))


# -- Approve ------------------------------------------------------------------------


def test_approving_names_places_and_logs(devices, admin, company, branch):
    device = pending(company)

    approve(devices, device, admin, branch)

    device.refresh_from_db()
    assert device.status == DeviceStatus.APPROVED
    assert device.name == "AlMamzar-POS1"
    assert (device.approved_by, device.approved_at is not None) == (admin, True)
    mapping = DeviceMapping.objects.get(device=device)
    assert (mapping.branch, mapping.to_date) == (branch, None)
    assert actions(device) == [DeviceAction.APPROVED]


def test_the_station_is_optional_at_approval(devices, admin, company):
    """Stations are Device Mapping's job; the tablet cannot log in until it has
    one (login step 9), but it can be approved without."""
    device = pending(company)
    approve(devices, device, admin, branch=None)

    assert Device.objects.get(pk=device.pk).status == DeviceStatus.APPROVED
    assert not DeviceMapping.objects.filter(device=device).exists()


def test_a_manager_device_may_have_no_station(devices, admin, company):
    device = pending(company, channel=Channel.MANAGER)
    approve(devices, device, admin, branch=None)
    assert Device.objects.get(pk=device.pk).status == DeviceStatus.APPROVED


def test_a_name_is_required(devices, admin, company, branch):
    with pytest.raises(services.DeviceActionError, match="name"):
        approve(devices, pending(company), admin, branch, name="  ")


def test_approving_twice_is_refused(devices, admin, company, branch, other_branch):
    device = pending(company)
    approve(devices, device, admin, branch)

    with pytest.raises(services.DeviceActionError, match="awaiting approval"):
        approve(devices, device, admin, other_branch)
    assert DeviceMapping.objects.filter(device=device).count() == 1


def test_one_device_per_station_unless_the_branch_allows_more(devices, admin, company, branch):
    approve(devices, pending(company, "a"), admin, branch, name="First")

    with pytest.raises(services.DeviceActionError, match="allows one device"):
        approve(devices, pending(company, "b"), admin, branch, name="Second")

    branch.is_multi_device = True
    branch.save()
    approve(devices, pending(company, "c"), admin, branch, name="Third")   # no error


def test_a_station_of_another_company_is_refused(devices, admin, company, branch):
    other = Company.objects.create(
        short_code="RIV", name="Rival", country=company.country, state=company.state,
        phone_number="+9713333333", email="r@rival.test",
    )
    theirs = Branch.objects.create(
        company=other, location=branch.location, short_code="R01", name="Rival 1",
        branch_type=BranchType.STATION,
    )
    with pytest.raises(services.DeviceActionError, match="active station"):
        approve(devices, pending(company), admin, theirs)


def test_another_companys_device_cannot_be_approved(admin, company, branch):
    other = Company.objects.create(
        short_code="RIV", name="Rival", country=company.country, state=company.state,
        phone_number="+9713333333", email="r@rival.test",
    )
    theirs = pending(other)
    with pytest.raises(services.DeviceActionError, match="not found"):
        approve(scoping.devices_for(admin), theirs, admin, branch)


# -- Approve as replacement ------------------------------------------------------------


def test_a_replacement_retires_the_old_tablet(devices, admin, company, branch):
    old = pending(company, "old")
    approve(devices, old, admin, branch, name="AlMamzar-POS1")
    session = live_session(old, branch)
    new = pending(company, "new", platform_id="after-reset")

    # The station allows one device; the one being replaced does not count.
    approve(devices, new, admin, branch, name="AlMamzar-POS1", replaces_pk=old.pk)

    old.refresh_from_db()
    new.refresh_from_db()
    assert old.status == DeviceStatus.RETIRED
    assert new.status == DeviceStatus.APPROVED and new.replaced_device == old
    assert DeviceMapping.objects.get(device=old).to_date is not None
    assert DeviceMapping.objects.get(device=new, to_date__isnull=True).branch == branch
    session.refresh_from_db()
    assert (session.logged_out_at is not None, session.logout_reason) == (True, LogoutReason.FORCED)
    assert actions(old) == [DeviceAction.RETIRED, DeviceAction.APPROVED]
    assert actions(new) == [DeviceAction.REPLACED]


def test_only_a_live_device_of_the_same_app_can_be_replaced(devices, admin, company, branch):
    manager = pending(company, "mgr", channel=Channel.MANAGER)
    approve(devices, manager, admin, None, name="Manager phone")

    with pytest.raises(services.DeviceActionError, match="same app"):
        approve(devices, pending(company, "new"), admin, branch, replaces_pk=manager.pk)


# -- Reconnect -------------------------------------------------------------------------


def test_reconnect_keeps_the_original_and_discards_the_request(devices, admin, company, branch):
    original = pending(company, "old-install")
    approve(devices, original, admin, branch)
    session = live_session(original, branch)
    number = original.device_registration_id
    request_row = pending(company, "new-install", reconnect_of=original)
    request_row.push_token = "fcm-new"
    request_row.save()

    services.reconnect_device(devices, request_row.pk, user=admin)

    original.refresh_from_db()
    assert original.installation_id == "new-install"
    assert original.device_registration_id == number
    assert (original.status, original.name, original.push_token) == (DeviceStatus.APPROVED, "AlMamzar-POS1", "fcm-new")
    assert not Device.objects.filter(pk=request_row.pk).exists()
    assert DeviceMapping.objects.get(device=original, to_date__isnull=True).branch == branch
    session.refresh_from_db()
    assert session.logged_out_at is not None
    assert actions(original)[0] == DeviceAction.RECONNECTED


def test_reconnect_keeps_a_blocked_original_blocked(devices, admin, company, branch):
    original = pending(company, "old-install")
    approve(devices, original, admin, branch)
    services.block_device(devices, original.pk, user=admin)
    request_row = pending(company, "new-install", reconnect_of=original)

    services.reconnect_device(devices, request_row.pk, user=admin)

    assert Device.objects.get(pk=original.pk).status == DeviceStatus.BLOCKED


def test_reconnect_needs_a_reinstall_request(devices, admin, company):
    with pytest.raises(services.DeviceActionError, match="not a reinstall"):
        services.reconnect_device(devices, pending(company).pk, user=admin)


def test_register_as_new_drops_the_link(devices, admin, company, branch, other_branch):
    original = pending(company, "old-install")
    approve(devices, original, admin, branch)
    request_row = pending(company, "new-install", reconnect_of=original)

    approve(devices, request_row, admin, other_branch, name="Second tablet")

    request_row.refresh_from_db()
    assert request_row.status == DeviceStatus.APPROVED and request_row.reconnect_of is None
    assert Device.objects.get(pk=original.pk).installation_id == "old-install"


# -- Reject, block, unblock ------------------------------------------------------------------


def test_reject_retires_with_the_reason(devices, admin, company):
    device = pending(company)
    services.reject_device(devices, device.pk, user=admin, reason="Personal phone")

    assert Device.objects.get(pk=device.pk).status == DeviceStatus.RETIRED
    assert DeviceStatusLog.objects.get(device=device).reason == "Personal phone"


def test_block_closes_sessions_and_keeps_the_station(devices, admin, company, branch):
    device = pending(company)
    approve(devices, device, admin, branch)
    session = live_session(device, branch)

    services.block_device(devices, device.pk, user=admin, reason="Stolen")

    session.refresh_from_db()
    assert session.logout_reason == LogoutReason.DEVICE_BLOCKED
    assert Device.objects.get(pk=device.pk).status == DeviceStatus.BLOCKED
    assert DeviceMapping.objects.filter(device=device, to_date__isnull=True).exists()


def test_unblock_restores_approval(devices, admin, company, branch):
    device = pending(company)
    approve(devices, device, admin, branch)
    services.block_device(devices, device.pk, user=admin)

    services.unblock_device(devices, device.pk, user=admin)

    assert Device.objects.get(pk=device.pk).status == DeviceStatus.APPROVED


def test_unblock_does_not_need_a_station(devices, admin, company, branch):
    device = pending(company)
    approve(devices, device, admin, branch)
    services.block_device(devices, device.pk, user=admin)
    DeviceMapping.objects.filter(device=device).update(to_date=timezone.now())

    services.unblock_device(devices, device.pk, user=admin)

    assert Device.objects.get(pk=device.pk).status == DeviceStatus.APPROVED


@pytest.mark.parametrize("act,status", [
    (services.block_device, DeviceStatus.PENDING),
    (services.unblock_device, DeviceStatus.APPROVED),
    (services.reject_device, DeviceStatus.APPROVED),
])
def test_an_action_on_the_wrong_status_is_refused(devices, admin, company, act, status):
    device = pending(company)
    Device.objects.filter(pk=device.pk).update(status=status, approved_at=timezone.now())
    with pytest.raises(services.DeviceActionError):
        act(devices, device.pk, user=admin)


# -- Endpoints -----------------------------------------------------------------------------


def post(client, url, payload=None):
    return client.post(url, json.dumps(payload or {}), content_type="application/json")


def test_approve_endpoint(signed_in, company, branch):
    device = pending(company)

    response = post(signed_in, f"/devices/approval/{device.pk}/approve/",
                    {"name": "AlMamzar-POS1", "branch": branch.pk})

    assert response.json() == {"ok": True, "message": "AlMamzar-POS1 approved."}
    assert Device.objects.get(pk=device.pk).status == DeviceStatus.APPROVED


def test_a_refusal_comes_back_as_a_message(signed_in, company):
    response = post(signed_in, f"/devices/approval/{pending(company).pk}/approve/", {"name": " "})

    assert response.status_code == 400
    assert response.json()["errors"][0]["field"] == "Name"


def test_actions_need_permission(no_permissions, company):
    device = pending(company)
    for action in ("approve", "reconnect", "reject", "block", "unblock"):
        assert post(no_permissions, f"/devices/approval/{device.pk}/{action}/").status_code == 403
    assert Device.objects.get(pk=device.pk).status == DeviceStatus.PENDING


def test_the_screen_carries_the_actions(signed_in, company):
    device = pending(company)
    html = signed_in.get("/devices/approval/").content.decode()

    assert f'data-device-action="approve" data-pk="{device.pk}"' in html
    assert 'data-scr-modal="device-approve"' in html
    assert "byky-device-approval.js" in html
    # No "replaces an existing device" picker; the station is optional.
    assert "data-approve-replaces" not in html
    assert "No station yet" in html


def test_the_pending_tab_looks_up_a_registration_number(signed_in, company):
    device = pending(company)
    html = signed_in.get("/devices/approval/").content.decode()

    assert 'placeholder="Registration number, e.g. 1003"' in html
    assert "data-dev-lookup" in html
    assert "data-dev-match data-can-approve" in html
    assert f'data-number="{device.device_registration_id}" data-state="pending"' in html


# -- The whole loop -------------------------------------------------------------------------


def register(client, installation_id, platform_id="fed25"):
    return client.post(
        "/api/v1/operator/device/registration",
        {"credentials": {}, "request_data": {
            "installation_id": installation_id, "platform": "android", "platform_id": platform_id,
        }},
        content_type="application/json",
    ).json()


def test_new_tablet_to_reinstall_and_back_to_its_own_number(client, signed_in, company, branch):
    first = register(client, "install-a")
    assert first["code"] == "pending_approval"
    number = first["data"]["device_registration_id"]
    device = Device.objects.get(device_registration_id=number)

    post(signed_in, f"/devices/approval/{device.pk}/approve/", {"name": "AlMamzar-POS1", "branch": branch.pk})
    approved = register(client, "install-a")
    assert (approved["code"], approved["data"]["device_registration_id"]) == ("approved", number)

    # The app is reinstalled: new installation, same phone.
    claim = register(client, "install-b")
    assert claim["code"] == "reconnect_pending"
    assert claim["data"]["matched_device"]["device_registration_id"] == number
    request_row = Device.objects.get(installation_id="install-b")

    post(signed_in, f"/devices/approval/{request_row.pk}/reconnect/")
    back = register(client, "install-b")
    assert (back["code"], back["data"]["device_registration_id"]) == ("approved", number)

    # The old installation no longer exists anywhere.
    assert register(client, "install-a")["code"] == "reconnect_pending"


def test_rejected_then_blocked_as_the_tablet_sees_it(client, signed_in, company, branch):
    register(client, "install-a")
    device = Device.objects.get(installation_id="install-a")
    post(signed_in, f"/devices/approval/{device.pk}/reject/", {"reason": "Unknown tablet"})
    assert register(client, "install-a")["code"] == "device_retired"

    register(client, "install-c", platform_id="other")
    second = Device.objects.get(installation_id="install-c")
    post(signed_in, f"/devices/approval/{second.pk}/approve/", {"name": "POS", "branch": branch.pk})
    post(signed_in, f"/devices/approval/{second.pk}/block/")
    assert register(client, "install-c", platform_id="other")["code"] == "device_blocked"

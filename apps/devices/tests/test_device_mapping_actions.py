"""Device Mapping's Add, Re-map and Close mapping.

design/03-login.md section 8.6: nobody types a date -- mapping is stamped when
it is saved, as the legacy did; intervals are half-open, a re-map closes the
open row at the moment the new one opens, and nothing moves while someone is
logged in on the device.
"""

import datetime
import json

import pytest
from django.utils import timezone

from apps.devices import scoping, services
from apps.devices.models import Device, DeviceMapping, DeviceStatus
from apps.portal.auth import open_session
from core.enums import Channel, UserScope
from core.models import User

OPERATOR = Channel.OPERATOR


@pytest.fixture
def admin(company):
    return User.objects.create_user(
        "admin.m", "Byky#2026", company=company, scope=UserScope.COMPANY,
        allowed_channels=[Channel.WEB],
    )


@pytest.fixture
def devices(admin):
    return scoping.devices_for(admin)


def tablet(company, installation_id="tab-1", status=DeviceStatus.APPROVED):
    return Device.objects.create(
        company=company, installation_id=installation_id, platform="android",
        channel=OPERATOR, status=status, name=f"POS-{installation_id}",
        approved_at=timezone.now(),
    )


def mapped(device, branch, days_ago=10):
    return DeviceMapping.objects.create(
        device=device, branch=branch, from_date=timezone.now() - datetime.timedelta(days=days_ago),
    )


# -- Services ---------------------------------------------------------------------------


def test_add_gives_an_unmapped_device_a_station_from_now(devices, admin, company, branch):
    device = tablet(company)
    before = timezone.now()
    services.map_device(devices, device.pk, user=admin, branch=branch)

    row = DeviceMapping.objects.get(device=device, to_date__isnull=True)
    assert row.branch == branch
    assert before <= row.from_date <= timezone.now()


def test_a_blocked_device_can_be_mapped(devices, admin, company, branch):
    """It must have a station before it can be unblocked."""
    device = tablet(company, status=DeviceStatus.BLOCKED)
    services.map_device(devices, device.pk, user=admin, branch=branch)
    services.unblock_device(devices, device.pk, user=admin)

    assert Device.objects.get(pk=device.pk).status == DeviceStatus.APPROVED


def test_add_refuses_a_device_that_already_has_a_station(devices, admin, company, branch, other_branch):
    device = tablet(company)
    mapped(device, branch)
    with pytest.raises(services.DeviceActionError, match="Re-map"):
        services.map_device(devices, device.pk, user=admin, branch=other_branch)


def test_remap_closes_the_old_row_at_the_moment_the_new_one_opens(devices, admin, company, branch, other_branch):
    device = tablet(company)
    old = mapped(device, branch)
    moment = timezone.now()

    services.remap_device(devices, device.pk, user=admin, branch=other_branch, now=moment)

    old.refresh_from_db()
    new = DeviceMapping.objects.get(device=device, to_date__isnull=True)
    assert old.to_date == moment == new.from_date       # half-open: no gap, no overlap
    assert (new.branch, old.modified_by) == (other_branch, admin)
    assert DeviceMapping.objects.filter(device=device).count() == 2


def test_two_moves_in_one_day_keep_their_order(devices, admin, company, branch, other_branch):
    """The reason for times rather than dates: 09:10 and 14:32 on one day are
    two different moves, not one date written twice."""
    device = tablet(company)
    mapped(device, branch, days_ago=0)
    morning = timezone.now().replace(hour=5, minute=10, second=0, microsecond=0) + datetime.timedelta(days=1)
    afternoon = morning + datetime.timedelta(hours=5, minutes=22)

    services.remap_device(devices, device.pk, user=admin, branch=other_branch, now=morning)
    services.remap_device(devices, device.pk, user=admin, branch=branch, now=afternoon)

    rows = list(DeviceMapping.objects.filter(device=device).order_by("from_date"))
    assert [r.branch for r in rows] == [branch, other_branch, branch]
    assert (rows[1].from_date, rows[1].to_date) == (morning, afternoon)


def test_remap_to_the_same_station_is_refused(devices, admin, company, branch):
    device = tablet(company)
    mapped(device, branch)
    with pytest.raises(services.DeviceActionError, match="already at this station"):
        services.remap_device(devices, device.pk, user=admin, branch=branch)


def test_remap_respects_one_device_per_station(devices, admin, company, branch, other_branch):
    holder = tablet(company, "holder")
    mapped(holder, other_branch)
    device = tablet(company)
    mapped(device, branch)

    with pytest.raises(services.DeviceActionError, match="allows one device"):
        services.remap_device(devices, device.pk, user=admin, branch=other_branch)


def test_close_ends_the_mapping_now(devices, admin, company, branch):
    device = tablet(company)
    row = mapped(device, branch)
    moment = timezone.now()

    services.close_mapping(devices, device.pk, user=admin, now=moment)

    row.refresh_from_db()
    assert row.to_date == moment
    assert not DeviceMapping.objects.filter(device=device, to_date__isnull=True).exists()


def test_nothing_moves_while_someone_is_logged_in(devices, admin, company, branch, other_branch):
    device = tablet(company)
    mapped(device, branch)
    operator = User.objects.create_user("op.x", "Byky#2026", company=company,
                                        scope=UserScope.COMPANY, allowed_channels=[OPERATOR])
    open_session(operator, OPERATOR, device=device, branch=branch)

    with pytest.raises(services.DeviceActionError, match="logged in"):
        services.remap_device(devices, device.pk, user=admin, branch=other_branch)
    with pytest.raises(services.DeviceActionError, match="logged in"):
        services.close_mapping(devices, device.pk, user=admin)


def test_a_pending_device_cannot_be_mapped(devices, admin, company, branch):
    device = tablet(company, status=DeviceStatus.PENDING)
    with pytest.raises(services.DeviceActionError, match="approved or blocked"):
        services.map_device(devices, device.pk, user=admin, branch=branch)


# -- Endpoints and screen ----------------------------------------------------------------------


def post(client, url, payload=None):
    return client.post(url, json.dumps(payload or {}), content_type="application/json")


def test_the_add_drawer_saves_without_a_date(signed_in, company, branch):
    device = tablet(company)

    response = post(signed_in, "/devices/mapping/save/", {"device": device.pk, "branch": branch.pk})

    assert response.json()["ok"] is True
    assert DeviceMapping.objects.get(device=device).from_date is not None


def test_remap_endpoint(signed_in, company, branch, other_branch):
    device = tablet(company)
    mapped(device, branch)

    response = post(signed_in, f"/devices/mapping/{device.pk}/remap/", {"branch": other_branch.pk})

    assert response.json()["ok"] is True
    assert DeviceMapping.objects.get(device=device, to_date__isnull=True).branch == other_branch


def test_close_endpoint(signed_in, company, branch):
    device = tablet(company)
    mapped(device, branch)

    assert post(signed_in, f"/devices/mapping/{device.pk}/close/").json()["ok"] is True


def test_a_missing_station_is_a_message_not_a_crash(signed_in, company, branch):
    device = tablet(company)
    mapped(device, branch)

    response = post(signed_in, f"/devices/mapping/{device.pk}/remap/", {})

    assert response.status_code == 400
    assert response.json()["errors"][0]["field"] == "Station"


def test_mapping_actions_need_permission(no_permissions, company, branch):
    device = tablet(company)
    mapped(device, branch)
    assert post(no_permissions, "/devices/mapping/save/", {"device": device.pk}).status_code == 403
    assert post(no_permissions, f"/devices/mapping/{device.pk}/remap/").status_code == 403
    assert post(no_permissions, f"/devices/mapping/{device.pk}/close/").status_code == 403


def test_the_screen_wires_remap_close_and_the_drawer(signed_in, company, branch):
    device = tablet(company)
    mapped(device, branch)
    unmapped = tablet(company, "tab-2")
    html = signed_in.get("/devices/mapping/").content.decode()

    assert f'data-mapping-action="remap" data-pk="{device.pk}"' in html
    assert f'data-mapping-action="close" data-pk="{device.pk}"' in html
    assert 'data-save-url="/devices/mapping/save/"' in html
    assert 'data-scr-modal="device-remap"' in html
    assert "byky-device-mapping.js" in html
    # Nobody types a date any more.
    assert "from_date" not in html
    assert "byky-datepicker.js" not in html
    # The Add drawer lists the unmapped device, not the mapped one.
    assert f"{unmapped.device_registration_id} — POS-tab-2" in html
    assert f"{device.device_registration_id} — POS-tab-1" not in html

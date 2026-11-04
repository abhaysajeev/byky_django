"""Device Mapping's Add, Re-map and Close mapping.

design/03-login.md section 8.6: nobody types a date -- mapping is stamped when
it is saved, as the legacy did; intervals are half-open, a re-map closes the
open row at the moment the new one opens. Re-map and Close mapping only on an
idle device: while someone is logged in they are refused (owner, 3 Nov 2026),
and a system admin can Force logout a lost tablet. Only RMS operator devices
have a station.
"""

import datetime
import json

import pytest
from django.utils import timezone

from apps.devices import scoping, services
from apps.devices.models import Device, DeviceMapping, DeviceStatus
from apps.portal.auth import open_session
from apps.portal.models import Role
from apps.portal.services import grant_all
from apps.portal.session_models import LogoutReason
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


def tablet(company, installation_id="tab-1", status=DeviceStatus.APPROVED, channel=OPERATOR):
    return Device.objects.create(
        company=company, installation_id=installation_id, platform="android",
        channel=channel, status=status, name=f"POS-{installation_id}",
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


def _signed_in(company, device, branch, username="op.x", *, expired=False):
    """A tablet login as the API makes it: open, with a refresh token valid
    for 30 days (or already expired)."""
    operator = User.objects.create_user(username, "Byky#2026", company=company,
                                        scope=UserScope.COMPANY, allowed_channels=[OPERATOR])
    session = open_session(operator, OPERATOR, device=device, branch=branch)
    session.refresh_expires_at = timezone.now() + datetime.timedelta(days=-1 if expired else 30)
    session.save(update_fields=["refresh_expires_at"])
    return session


def test_remap_is_refused_while_someone_is_logged_in(devices, admin, company, branch, other_branch):
    """31 Oct 2026: a remap closed a live session under the operator and the
    app stuck on session_closed. Now they log out first."""
    device = tablet(company)
    mapped(device, branch)
    session = _signed_in(company, device, branch)

    with pytest.raises(services.DeviceActionError, match="op.x is logged in"):
        services.remap_device(devices, device.pk, user=admin, branch=other_branch)

    session.refresh_from_db()
    assert session.logged_out_at is None
    assert DeviceMapping.objects.get(device=device, to_date__isnull=True).branch == branch


def test_close_is_refused_while_someone_is_logged_in(devices, admin, company, branch):
    device = tablet(company)
    mapped(device, branch)
    session = _signed_in(company, device, branch)

    with pytest.raises(services.DeviceActionError, match="must log out first"):
        services.close_mapping(devices, device.pk, user=admin)

    session.refresh_from_db()
    assert session.logged_out_at is None
    assert DeviceMapping.objects.filter(device=device, to_date__isnull=True).exists()


def test_an_expired_session_does_not_block_a_remap(devices, admin, company, branch, other_branch):
    """A tablet switched off for weeks: its session never closed but has
    expired. The remap goes ahead and closes it."""
    device = tablet(company)
    mapped(device, branch)
    session = _signed_in(company, device, branch, expired=True)

    services.remap_device(devices, device.pk, user=admin, branch=other_branch)

    session.refresh_from_db()
    assert session.logout_reason == LogoutReason.FORCED
    assert DeviceMapping.objects.get(device=device, to_date__isnull=True).branch == other_branch


def test_force_logout_signs_out_then_the_remap_works(devices, admin, company, branch, other_branch):
    device = tablet(company)
    mapped(device, branch)
    session = _signed_in(company, device, branch)

    assert services.force_logout(devices, device.pk, user=admin) == 1
    services.remap_device(devices, device.pk, user=admin, branch=other_branch)

    session.refresh_from_db()
    assert session.logout_reason == LogoutReason.FORCED
    with pytest.raises(services.DeviceActionError, match="Nobody is logged in"):
        services.force_logout(devices, device.pk, user=admin)


@pytest.mark.parametrize("channel", [Channel.EMPLOYEE, Channel.MANAGER])
def test_only_operator_devices_get_a_station(devices, admin, company, branch, channel):
    phone = tablet(company, "phone-1", channel=channel)

    with pytest.raises(services.DeviceActionError, match="Only RMS operator devices"):
        services.map_device(devices, phone.pk, user=admin, branch=branch)


@pytest.mark.parametrize("channel", [Channel.EMPLOYEE, Channel.MANAGER])
def test_approving_a_phone_with_a_station_is_refused(devices, admin, company, branch, channel):
    phone = tablet(company, "phone-2", status=DeviceStatus.PENDING, channel=channel)

    with pytest.raises(services.DeviceActionError, match="Only RMS operator devices"):
        services.approve_device(devices, phone.pk, user=admin, name="Staff phone", branch=branch)

    services.approve_device(devices, phone.pk, user=admin, name="Staff phone")       # without: fine
    phone.refresh_from_db()
    assert phone.status == DeviceStatus.APPROVED


def test_a_refused_remap_logs_nobody_out(devices, admin, company, branch):
    device = tablet(company)
    mapped(device, branch)
    session = _signed_in(company, device, branch)

    with pytest.raises(services.DeviceActionError, match="already at this station"):
        services.remap_device(devices, device.pk, user=admin, branch=branch)

    session.refresh_from_db()
    assert session.logged_out_at is None


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


def test_the_screen_lists_operator_devices_only(signed_in, company, branch):
    tablet(company, "tab-op")
    phone = tablet(company, "phone-emp", channel=Channel.EMPLOYEE)

    html = signed_in.get("/devices/mapping/").content.decode()

    assert "POS-tab-op" in html
    assert f'data-pk="{phone.pk}"' not in html and "POS-phone-emp" not in html


def test_remap_and_close_are_greyed_and_say_who_while_logged_in(signed_in, company, branch):
    device = tablet(company)
    mapped(device, branch)
    _signed_in(company, device, branch)

    html = signed_in.get("/devices/mapping/").content.decode()

    assert html.count('disabled title="op.x is logged in — they must log out first"') == 2
    assert f'data-mapping-action="force-logout" data-pk="{device.pk}"' in html   # Force logout instead
    assert ".scr-menu-item:disabled" in open("static/css/byky-screen.css").read()


@pytest.fixture
def system_admin(client, db):
    role = Role.objects.create(company=None, name="System Administrator")
    grant_all(role)
    User.objects.create_user("platform.admin", "Byky#2026", display_name="Platform", scope=UserScope.SYSTEM,
                             role=role, allowed_channels=[Channel.WEB])
    client.post("/login/", {"username": "platform.admin", "password": "Byky#2026"})
    return client


# Force logout: any role with Device Mapping update (owner, 4 Nov 2026 -- it was system
# admins only), within that user's own company.


def test_a_company_administrator_sees_and_uses_force_logout(signed_in, company, branch):
    device = tablet(company)
    mapped(device, branch)
    session = _signed_in(company, device, branch)

    html = signed_in.get("/devices/mapping/").content.decode()
    assert f'data-mapping-action="force-logout" data-pk="{device.pk}"' in html

    response = post(signed_in, f"/devices/mapping/{device.pk}/force-logout/")

    assert response.status_code == 200 and response.json()["ok"], response.content
    session.refresh_from_db()
    assert session.logout_reason == LogoutReason.FORCED


def test_force_logout_needs_device_mapping_update(company, branch):
    from django.test import Client

    from apps.portal.models import RolePermission

    role = Role.objects.create(company=company, name="Viewer")
    grant_all(role)
    RolePermission.objects.filter(role=role, page__code="devices.device_mapping").update(can_update=False)
    User.objects.create_user("viewer.x", "Byky#2026", company=company, role=role, allowed_channels=[Channel.WEB])
    viewer = Client()
    viewer.post("/login/", {"username": "viewer.x", "password": "Byky#2026"})
    device = tablet(company)
    mapped(device, branch)
    session = _signed_in(company, device, branch)

    assert 'data-mapping-action="force-logout"' not in viewer.get("/devices/mapping/").content.decode()
    assert post(viewer, f"/devices/mapping/{device.pk}/force-logout/").status_code == 403
    session.refresh_from_db()
    assert session.logged_out_at is None


def test_force_logout_never_reaches_another_companys_tablet(signed_in, company):
    from apps.company.models import Company

    other = Company.objects.create(short_code="OTH", name="Other", country=company.country, state=company.state,
                                   phone_number="+9710000001", email="ops@other.test")
    device = tablet(other, "tab-other")
    session = _signed_in(other, device, None, "op.other")

    response = post(signed_in, f"/devices/mapping/{device.pk}/force-logout/")

    assert not (response.status_code == 200 and response.json().get("ok")), response.content
    session.refresh_from_db()
    assert session.logged_out_at is None


def test_a_system_admin_sees_and_uses_force_logout(system_admin, company, branch):
    device = tablet(company)
    mapped(device, branch)
    session = _signed_in(company, device, branch)

    html = system_admin.get("/devices/mapping/").content.decode()
    assert f'data-mapping-action="force-logout" data-pk="{device.pk}"' in html

    response = post(system_admin, f"/devices/mapping/{device.pk}/force-logout/")

    assert response.status_code == 200 and response.json()["ok"], response.content
    session.refresh_from_db()
    assert session.logout_reason == LogoutReason.FORCED

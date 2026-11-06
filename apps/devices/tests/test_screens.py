"""The Device screens with real devices in them: status, sessions, mappings.

That every screen renders on an empty database, refuses a role without it and
sends a stranger to sign in is apps/portal/tests/test_every_screen.py.
"""

import datetime

import pytest
from django.utils import timezone

from apps.devices.models import Device, DeviceMapping, DeviceStatus
from apps.portal.session_models import AppSession
from core.enums import Channel
from core.models import User


@pytest.fixture
def approved_device(company, branch):
    device = Device.objects.create(
        company=company,
        installation_id="9f97de87-4b28-48ca-87b7-f00410f1e0c2",
        platform="android",
        platform_id="fed25f408ca56786",
        device_model="Samsung SM-A556E",
        channel=Channel.OPERATOR,
        name="Corniche-POS1",
        status=DeviceStatus.APPROVED,
        approved_at=timezone.now(),
    )
    device.refresh_from_db()
    DeviceMapping.objects.create(
        device=device, branch=branch, from_date=datetime.date(2026, 9, 1)
    )
    return device


def test_a_pending_device_waits_on_the_approval_screen(signed_in, company):
    device = Device.objects.create(
        company=company,
        installation_id="pending-install-uuid",
        platform="android",
        device_model="Redmi Note 12",
        channel=Channel.OPERATOR,
    )
    device.refresh_from_db()

    content = signed_in.get("/devices/approval/").content.decode()

    assert str(device.device_registration_id) in content
    assert "Redmi Note 12" in content
    # It is not yet a mappable device.
    assert "Redmi Note 12" not in signed_in.get("/devices/mapping/").content.decode()


def test_an_approved_device_shows_its_station_and_reads_logged_out(signed_in, approved_device):
    content = signed_in.get("/devices/mapping/").content.decode()

    assert "Corniche-POS1" in content
    assert "Corniche 1" in content
    assert "Logged out" in content and "Idle" not in content
    # Platform ID is a grid column (owner, 4 Nov 2026); App Version and
    # Mapped Since are not.
    assert "fed25f408ca56786" in content
    assert "<th>Platform ID</th>" in content
    assert "<th>App Version</th>" not in content and "<th>Mapped Since</th>" not in content


def test_an_open_session_makes_a_device_read_logged_in(signed_in, approved_device, company, branch):
    """Last Login, Employee and Status all come from the session, which is what
    the legacy grid computed too. App Version is no longer a column (owner,
    4 Nov 2026)."""
    operator = User.objects.create_user(
        "op.erator", "Byky#2026", display_name="Ahmed R", company=company,
        allowed_channels=[Channel.OPERATOR],
    )
    AppSession.objects.create(
        user=operator, channel=Channel.OPERATOR, device=approved_device, branch=branch,
        logged_in_at=timezone.now(), app_version="1.12.80",
        refresh_expires_at=timezone.now() + datetime.timedelta(days=1),
    )

    content = signed_in.get("/devices/mapping/").content.decode()

    assert "Logged in" in content and "In use" not in content
    assert "Ahmed R" in content
    assert "1.12.80" not in content


def test_a_closed_session_leaves_the_device_logged_out(signed_in, approved_device, company, branch):
    operator = User.objects.create_user(
        "op.erator", "Byky#2026", display_name="Ahmed R", company=company,
        allowed_channels=[Channel.OPERATOR],
    )
    AppSession.objects.create(
        user=operator, channel=Channel.OPERATOR, device=approved_device, branch=branch,
        logged_in_at=timezone.now(), logged_out_at=timezone.now(), app_version="1.12.80",
    )

    content = signed_in.get("/devices/mapping/").content.decode()

    assert "Logged out" in content
    # The last login is still worth showing; only the status changed.
    assert "Ahmed R" in content


def test_the_detail_page_is_reached_by_the_number_on_the_tablet(signed_in, approved_device):
    response = signed_in.get(f"/devices/approval/{approved_device.device_registration_id}/")

    assert response.status_code == 200
    content = response.content.decode()
    assert "Corniche-POS1" in content
    assert approved_device.installation_id in content


def test_a_closed_mapping_shows_its_to_date_on_the_device(signed_in, approved_device, other_branch):
    """The mapping list only ever shows the open row, whose to_date is empty by
    definition. A move's end date lives in the device's station history, and the
    list links to it."""
    open_row = approved_device.mappings.get(to_date__isnull=True)
    moved_on = datetime.date(2026, 10, 1)
    open_row.to_date = moved_on
    open_row.save(update_fields=["to_date"])
    DeviceMapping.objects.create(
        device=approved_device, branch=other_branch, from_date=moved_on
    )

    detail = signed_in.get(
        f"/devices/approval/{approved_device.device_registration_id}/"
    ).content.decode()

    assert "01 Oct 2026" in detail          # the closed row's To date
    assert "Corniche 1" in detail           # where it used to be
    assert "Marina 1" in detail             # where it is now
    assert "Current" in detail              # the open row says so instead of a date

    # And the mapping screen can reach that history.
    mapping = signed_in.get("/devices/mapping/").content.decode()
    assert f"/devices/approval/{approved_device.device_registration_id}/" in mapping


def test_an_unknown_number_says_so_rather_than_breaking(signed_in):
    response = signed_in.get("/devices/approval/999999/")

    assert response.status_code == 200
    assert "No device 999999" in response.content.decode()


# -- Device Approval: Login status and Force logout (owner, 7 Nov 2026) --------------


def _session(device, company, branch, username, channel=Channel.OPERATOR):
    person = User.objects.create_user(username, "Byky#2026", display_name=username.title(), company=company,
                                      allowed_channels=[channel])
    return AppSession.objects.create(
        user=person, channel=channel, device=device, branch=branch if channel == Channel.OPERATOR else None,
        logged_in_at=timezone.now(), refresh_expires_at=timezone.now() + datetime.timedelta(days=1),
    )


def _approved_tab(content):
    start = content.index('data-tabgroup="device-approval" data-tab="approved"')
    return content[start:content.index('data-tabgroup="device-approval" data-tab="blocked"')]


def test_the_approved_tab_shows_login_status_and_no_station(signed_in, approved_device):
    tab = _approved_tab(signed_in.get("/devices/approval/").content.decode())
    assert "<th>Login Status</th>" in tab and "<th>Station</th>" not in tab
    assert "Logged out" in tab and 'data-device-action="logout"' not in tab


def test_a_logged_in_device_reads_logged_in_and_offers_force_logout(signed_in, approved_device, company, branch):
    _session(approved_device, company, branch, "op.erator")
    tab = _approved_tab(signed_in.get("/devices/approval/").content.decode())
    assert "Logged in" in tab and "Op.Erator" in tab
    assert 'data-device-action="logout"' in tab


@pytest.mark.parametrize("channel", [Channel.OPERATOR, Channel.MANAGER])
def test_force_logout_from_approval_closes_the_session_of_any_app(signed_in, approved_device, company, branch,
                                                                  channel):
    Device.objects.filter(pk=approved_device.pk).update(channel=channel)
    session = _session(approved_device, company, branch, "some.one", channel)

    body = signed_in.post(f"/devices/approval/{approved_device.pk}/logout/", {},
                          content_type="application/json").json()

    assert body["ok"] is True
    session.refresh_from_db()
    assert session.logged_out_at is not None


def test_force_logout_needs_someone_logged_in(signed_in, approved_device):
    body = signed_in.post(f"/devices/approval/{approved_device.pk}/logout/", {},
                          content_type="application/json").json()
    assert body["ok"] is False and "Nobody is logged in" in body["errors"][0]["message"]


def test_force_logout_needs_device_approval_update(no_permissions, approved_device, company, branch):
    session = _session(approved_device, company, branch, "op.erator")
    response = no_permissions.post(f"/devices/approval/{approved_device.pk}/logout/", {},
                                   content_type="application/json")
    assert response.status_code == 403
    session.refresh_from_db()
    assert session.logged_out_at is None

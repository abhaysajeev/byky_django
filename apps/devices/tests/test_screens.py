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


def test_an_approved_device_shows_its_station_and_reads_idle(signed_in, approved_device):
    content = signed_in.get("/devices/mapping/").content.decode()

    assert "Corniche-POS1" in content
    assert "Corniche 1" in content
    assert "Idle" in content


def test_an_open_session_makes_a_device_read_in_use(signed_in, approved_device, company, branch):
    """App Version, Last Login, Employee and Status all come from the session,
    which is what the legacy grid computed too."""
    operator = User.objects.create_user(
        "op.erator", "Byky#2026", display_name="Ahmed R", company=company,
        allowed_channels=[Channel.OPERATOR],
    )
    AppSession.objects.create(
        user=operator, channel=Channel.OPERATOR, device=approved_device, branch=branch,
        logged_in_at=timezone.now(), app_version="1.12.80",
    )

    content = signed_in.get("/devices/mapping/").content.decode()

    assert "In use" in content
    assert "Ahmed R" in content
    assert "1.12.80" in content


def test_a_closed_session_leaves_the_device_idle(signed_in, approved_device, company, branch):
    operator = User.objects.create_user(
        "op.erator", "Byky#2026", display_name="Ahmed R", company=company,
        allowed_channels=[Channel.OPERATOR],
    )
    AppSession.objects.create(
        user=operator, channel=Channel.OPERATOR, device=approved_device, branch=branch,
        logged_in_at=timezone.now(), logged_out_at=timezone.now(), app_version="1.12.80",
    )

    content = signed_in.get("/devices/mapping/").content.decode()

    assert "Idle" in content
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

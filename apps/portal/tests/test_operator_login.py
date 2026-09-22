"""apps/portal/auth.py::sign_in_operator -- design/03-login.md section 4,
extended with the device steps the Employee app doesn't need (it has no
device concept at all -- this channel is the station till).

Every legacy StatusFlag this replaces is cited in
design/login/login-for-operator.md's own mapping table; this file is the
proof each one actually refuses (or succeeds) the way that table says.
"""

import pytest
from django.utils import timezone

from apps.company.models import Branch, BranchType, Company, Country, Location, State
from apps.crew.models import Designation, Employee
from apps.devices.models import Device, DeviceMapping, DeviceSettings, DeviceStatus
from apps.portal import auth
from apps.portal.models import Role
from apps.portal.services import grant_all
from apps.portal.session_models import AppSession
from core.enums import ApprovalStatus, Channel
from core.models import User

PASSWORD = "Byky#2026"


@pytest.fixture
def world(db):
    country = Country.objects.create(short_code="AE", name="United Arab Emirates")
    state = State.objects.create(country=country, short_code="AUH", name="Abu Dhabi")
    company = Company.objects.create(
        short_code="BYKY", name="BYKY", country=country, state=state,
        phone_number="+9710000000", email="ops@byky.test",
    )
    location = Location.objects.create(
        country=country, state=state, short_code="CORN", name="Corniche",
    )
    branch = Branch.objects.create(
        company=company, location=location, short_code="AUH01",
        name="Creek Park 1", branch_type=BranchType.STATION,
    )
    other_branch = Branch.objects.create(
        company=company, location=location, short_code="AUH02",
        name="Zabeel Park 1", branch_type=BranchType.STATION,
    )
    designation = Designation.objects.create(company=company, code="CASH", title="Cashier")
    role = Role.objects.create(company=company, name="Operator")
    grant_all(role)
    return {
        "company": company, "branch": branch, "other_branch": other_branch,
        "designation": designation, "role": role,
    }


@pytest.fixture
def employee(world):
    return Employee.objects.create(
        company=world["company"], employee_code="OPR001",
        first_name="Rashed", last_name="K", designation=world["designation"],
    )


@pytest.fixture
def user(world, employee):
    return User.objects.create_user(
        "OPR001", PASSWORD, display_name="Rashed K",
        company=world["company"], role=world["role"], employee=employee,
        allowed_channels=[Channel.OPERATOR],
    )


@pytest.fixture
def device(world):
    return Device.objects.create(
        company=world["company"], installation_id="till-1", platform="android",
        channel=Channel.OPERATOR, status=DeviceStatus.APPROVED, approved_at=timezone.now(),
    )


@pytest.fixture
def mapped_device(device, world):
    DeviceMapping.objects.create(device=device, branch=world["branch"], from_date=timezone.now())
    return device


@pytest.fixture
def device_settings(world):
    return DeviceSettings.objects.create(
        branch=world["branch"], settings_code="S01", order_no_prefix="CREEK",
        header_1="BYKY", header_2="Creek Park", footer_1="Thank you",
        approval_status=ApprovalStatus.APPROVED,
    )


@pytest.fixture
def ready_device(mapped_device, device_settings):
    """A device that should pass every check: approved, mapped, settings done."""
    return mapped_device


def sign_in(user, device_row, **kwargs):
    return auth.sign_in_operator(
        user.username, PASSWORD, device_row.installation_id if device_row else "unknown", **kwargs,
    )


# -- Success --------------------------------------------------------------------


def test_a_ready_device_signs_in(user, ready_device, world):
    signed_in_user, session, device, branch = sign_in(user, ready_device)
    assert signed_in_user.pk == user.pk
    assert session.channel == Channel.OPERATOR
    assert session.device_id == ready_device.pk
    assert session.branch_id == world["branch"].pk
    assert branch.pk == world["branch"].pk


def test_signing_in_replaces_a_session_on_the_same_device(user, ready_device):
    sign_in(user, ready_device)
    first = AppSession.objects.get(user=user, logged_out_at__isnull=True)

    sign_in(user, ready_device)

    first.refresh_from_db()
    assert first.logged_out_at is not None
    assert AppSession.objects.filter(user=user, logged_out_at__isnull=True).count() == 1


# -- Credentials and account checks (1-6), reused from check_credentials/check_account ---


def test_wrong_password_is_refused(user, ready_device):
    with pytest.raises(auth.LoginRefused) as refused:
        auth.sign_in_operator(user.username, "wrong", ready_device.installation_id)
    assert refused.value.code == "invalid_credentials"


def test_a_user_without_the_operator_channel_is_refused(world, employee, ready_device):
    other = User.objects.create_user(
        "OPR002", PASSWORD, display_name="No Till", company=world["company"],
        role=world["role"], employee=employee, allowed_channels=[Channel.WEB],
    )
    with pytest.raises(auth.LoginRefused) as refused:
        auth.sign_in_operator(other.username, PASSWORD, ready_device.installation_id)
    assert refused.value.code == "wrong_channel"


def test_a_blocked_employee_is_refused(user, employee, ready_device):
    employee.is_blocked = True
    employee.save(update_fields=["is_blocked"])
    with pytest.raises(auth.LoginRefused) as refused:
        sign_in(user, ready_device)
    assert refused.value.code == "user_blocked"


def test_a_user_with_no_linked_employee_is_refused(world, ready_device):
    """The legacy proc's own query INNER JOINs HrmsEmployee -- an account
    with no linked employee could never match it at all."""
    no_employee = User.objects.create_user(
        "OPR003", PASSWORD, display_name="No Employee", company=world["company"],
        role=world["role"], allowed_channels=[Channel.OPERATOR],
    )
    with pytest.raises(auth.LoginRefused) as refused:
        auth.sign_in_operator(no_employee.username, PASSWORD, ready_device.installation_id)
    assert refused.value.code == "invalid_credentials"


# -- Device steps (7-9a), new for this channel -----------------------------------


def test_an_unregistered_device_is_refused(user):
    with pytest.raises(auth.LoginRefused) as refused:
        auth.sign_in_operator(user.username, PASSWORD, "never-registered")
    assert refused.value.code == "device_not_registered"


def test_a_pending_device_is_refused(user, world):
    device = Device.objects.create(
        company=world["company"], installation_id="pending-till", platform="android",
        channel=Channel.OPERATOR,
    )
    with pytest.raises(auth.LoginRefused) as refused:
        sign_in(user, device)
    assert refused.value.code == "device_pending_approval"


def test_a_blocked_device_is_refused(user, world):
    device = Device.objects.create(
        company=world["company"], installation_id="blocked-till", platform="android",
        channel=Channel.OPERATOR, status=DeviceStatus.BLOCKED, approved_at=timezone.now(),
    )
    with pytest.raises(auth.LoginRefused) as refused:
        sign_in(user, device)
    assert refused.value.code == "device_blocked"


def test_a_retired_device_is_refused(user, world):
    device = Device.objects.create(
        company=world["company"], installation_id="retired-till", platform="android",
        channel=Channel.OPERATOR, status=DeviceStatus.RETIRED, approved_at=timezone.now(),
    )
    with pytest.raises(auth.LoginRefused) as refused:
        sign_in(user, device)
    assert refused.value.code == "device_retired"


def test_an_unmapped_device_is_refused(user, device):
    """Approved, but never given a station."""
    with pytest.raises(auth.LoginRefused) as refused:
        sign_in(user, device)
    assert refused.value.code == "device_not_mapped"


def test_a_mapped_branch_with_no_settings_is_refused(user, mapped_device):
    """New refusal code: the legacy's StatusFlag 11, 'Device Settings not
    done', which design/03-login.md section 9.2's table has no equivalent
    for -- confirmed by reading that table directly."""
    with pytest.raises(auth.LoginRefused) as refused:
        sign_in(user, mapped_device)
    assert refused.value.code == "device_settings_not_done"


# -- Step 10: same device vs. a different one, the one genuinely new check ------


def test_an_open_session_on_another_device_is_refused(user, ready_device, world, device_settings):
    sign_in(user, ready_device)

    second_device = Device.objects.create(
        company=world["company"], installation_id="till-2", platform="android",
        channel=Channel.OPERATOR, status=DeviceStatus.APPROVED, approved_at=timezone.now(),
    )
    DeviceMapping.objects.create(device=second_device, branch=world["branch"], from_date=timezone.now())

    with pytest.raises(auth.LoginRefused) as refused:
        sign_in(user, second_device)
    assert refused.value.code == "session_active_elsewhere"

    # And the first device's session is untouched -- refused, not replaced.
    assert AppSession.objects.filter(user=user, device=ready_device, logged_out_at__isnull=True).exists()

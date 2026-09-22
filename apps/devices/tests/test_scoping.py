"""A company sees its own devices and no one else's.

The documented way shared-database multi-tenancy leaks is one query that forgot
its filter, so every list on these screens goes through core.scoping.scoped_to
and every one of them is tested.
"""

import datetime

import pytest
from django.utils import timezone

from apps.company.models import Branch, BranchType, Company, Country, Location, State
from apps.devices import scoping
from apps.devices.models import Device, DeviceMapping, DeviceStatus
from core.enums import Channel, UserScope
from core.models import User


@pytest.fixture
def other_company(db):
    country = Country.objects.get_or_create(short_code="AE", name="United Arab Emirates")[0]
    state = State.objects.get_or_create(country=country, short_code="DXB", name="Dubai")[0]
    company = Company.objects.create(
        short_code="RIVAL", name="Rival Rentals", country=country, state=state,
        phone_number="+9711111111", email="ops@rival.test",
    )
    location = Location.objects.create(
        country=country, state=state, short_code="JUM", name="Jumeirah"
    )
    branch = Branch.objects.create(
        company=company, location=location, short_code="DXB01", name="Jumeirah 1",
        branch_type=BranchType.STATION,
    )
    device = Device.objects.create(
        company=company, installation_id="rival-install-uuid", platform="android",
        channel=Channel.OPERATOR, name="Rival-POS1", status=DeviceStatus.APPROVED,
        approved_at=timezone.now(),
    )
    DeviceMapping.objects.create(
        device=device, branch=branch, from_date=datetime.date(2026, 9, 1)
    )
    return company


@pytest.fixture
def own_device(company, branch):
    device = Device.objects.create(
        company=company, installation_id="own-install-uuid", platform="android",
        channel=Channel.OPERATOR, name="Corniche-POS1", status=DeviceStatus.APPROVED,
        approved_at=timezone.now(),
    )
    DeviceMapping.objects.create(
        device=device, branch=branch, from_date=datetime.date(2026, 9, 1)
    )
    return device


def test_a_company_user_sees_only_their_own_devices(company, own_device, other_company):
    user = User.objects.create_user(
        "sara.k", "Byky#2026", company=company, scope=UserScope.COMPANY,
        allowed_channels=[Channel.WEB],
    )

    assert list(scoping.devices_for(user)) == [own_device]
    assert scoping.mappings_for(user).count() == 1


def test_a_system_user_sees_every_company(company, own_device, other_company):
    user = User.objects.create_user(
        "sys.admin", "Byky#2026", scope=UserScope.SYSTEM, allowed_channels=[Channel.WEB],
    )

    assert scoping.devices_for(user).count() == 2
    assert scoping.mappings_for(user).count() == 2


def test_a_company_user_with_no_company_sees_nothing(company, own_device, other_company):
    """Fails closed. A misconfigured account must not fall back to seeing
    everything -- absence of data should never grant power.

    The user table will not hold such a row: `user_scope_matches_company`
    refuses it, which is the first line of this defence. The filter is checked
    here against an unsaved instance, so the second line is known to hold too if
    that constraint is ever relaxed.
    """
    orphan = User(username="orphan", scope=UserScope.COMPANY, company=None)

    assert scoping.devices_for(orphan).count() == 0
    assert scoping.mappings_for(orphan).count() == 0
    assert scoping.device_sessions_for(orphan).count() == 0


def test_the_screens_show_only_the_signed_in_company_rows(signed_in, own_device, other_company):
    content = signed_in.get("/devices/mapping/").content.decode()

    assert "Corniche-POS1" in content
    assert "Rival-POS1" not in content


# -- Releases, settings and bill counters ------------------------------------


def _catalogue(company, branch, code, device_name):
    from apps.devices.models import (
        AppRelease, AppReleaseMapping, BillContinuity, BillKind, DeviceSettings, ReleaseScope,
    )

    rel = AppRelease.objects.create(
        company=company, channel=Channel.OPERATOR, version_name=f"1.12.{code}",
        version_code=code, download_url="https://files.byky.test/a.apk",
    )
    AppReleaseMapping.objects.create(
        release=rel, company=company, channel=Channel.OPERATOR,
        scope=ReleaseScope.BRANCH, branch=branch,
    )
    DeviceSettings.objects.create(
        branch=branch, settings_code=f"S{code}", order_no_prefix=f"P{code}",
        header_1="H", header_2="H", footer_1="F",
    )
    device = Device.objects.create(
        company=company, installation_id=f"install-{device_name}", platform="android",
        channel=Channel.OPERATOR, name=device_name,
    )
    BillContinuity.objects.create(
        device=device, branch=branch, kind=BillKind.ORDER, prefix=f"P{code}",
    )


def test_a_company_sees_only_its_own_releases_settings_and_counters(company, branch, other_company):
    rival_branch = Branch.objects.get(short_code="DXB01")
    _catalogue(company, branch, 80, "own")
    _catalogue(other_company, rival_branch, 90, "rival")
    user = User.objects.create_user(
        "sara.k", "Byky#2026", company=company, scope=UserScope.COMPANY,
        allowed_channels=[Channel.WEB],
    )

    assert [r.version_code for r in scoping.releases_for(user)] == [80]
    assert [m.release.version_code for m in scoping.release_mappings_for(user)] == [80]
    assert [s.settings_code for s in scoping.settings_for(user)] == ["S80"]
    assert [c.prefix for c in scoping.bill_counters_for(user)] == ["P80"]


def test_a_system_user_sees_every_companys_releases(company, branch, other_company):
    rival_branch = Branch.objects.get(short_code="DXB01")
    _catalogue(company, branch, 80, "own")
    _catalogue(other_company, rival_branch, 90, "rival")
    user = User.objects.create_user(
        "sys.admin", "Byky#2026", scope=UserScope.SYSTEM, allowed_channels=[Channel.WEB],
    )

    assert scoping.releases_for(user).count() == 2
    assert scoping.settings_for(user).count() == 2
    assert scoping.bill_counters_for(user).count() == 2


def test_a_company_user_with_no_company_sees_no_releases_or_settings():
    orphan = User(username="orphan", scope=UserScope.COMPANY, company=None)

    assert scoping.releases_for(orphan).count() == 0
    assert scoping.release_mappings_for(orphan).count() == 0
    assert scoping.settings_for(orphan).count() == 0
    assert scoping.bill_counters_for(orphan).count() == 0

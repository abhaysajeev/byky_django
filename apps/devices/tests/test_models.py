"""The rules the database itself holds for devices and their mappings.

Each test states the rule it protects, because a constraint with no test is a
comment that happens to be in SQL.
"""

import datetime

import pytest
from django.db import IntegrityError, transaction

from apps.devices.models import Device, DeviceMapping, DeviceStatus
from core.enums import Channel

# A mapping is a moment, stamped when it is saved (devices/0010).
TODAY = datetime.datetime(2026, 9, 19, 9, 10, tzinfo=datetime.UTC)


def make_device(company, **extra):
    fields = {
        "company": company,
        "installation_id": "9f97de87-4b28-48ca-87b7-f00410f1e0c2",
        "platform": "android",
        "platform_id": "fed25f408ca56786",
        "device_model": "Samsung SM-A556E",
        "channel": Channel.OPERATOR,
    }
    fields.update(extra)
    device = Device.objects.create(**fields)
    device.refresh_from_db()
    return device


def test_a_registration_number_is_issued_on_insert(company):
    """The tablet is told its number before anyone approves it, so it has one
    from the moment the row exists."""
    device = make_device(company)

    assert device.device_registration_id >= 1000
    assert device.status == DeviceStatus.PENDING


def test_registration_numbers_are_not_the_primary_key(company):
    """Two identifiers, two jobs. The number survives a key change; the key is
    never shown to an app."""
    first = make_device(company)
    second = make_device(company, installation_id="second-install-uuid")

    assert first.device_registration_id != second.device_registration_id
    assert second.device_registration_id > first.device_registration_id


def test_an_installation_id_is_claimed_once(company):
    make_device(company)

    with pytest.raises(IntegrityError):
        make_device(company)


def test_a_device_may_hold_only_one_open_mapping(company, branch, other_branch):
    """Without this the question "which station is it at?" has no single
    answer -- the legacy asked it five different ways."""
    device = make_device(company, status=DeviceStatus.APPROVED,
                         approved_at=datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC))
    DeviceMapping.objects.create(device=device, branch=branch, from_date=TODAY)

    with pytest.raises(IntegrityError):
        DeviceMapping.objects.create(device=device, branch=other_branch, from_date=TODAY)


def test_re_mapping_closes_the_old_row_and_keeps_the_history(company, branch, other_branch):
    """A move is two rows, never an edit -- which is what the legacy did."""
    device = make_device(company, status=DeviceStatus.APPROVED,
                         approved_at=datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC))
    first = DeviceMapping.objects.create(device=device, branch=branch, from_date=TODAY)

    moved_on = TODAY + datetime.timedelta(days=30)
    first.to_date = moved_on
    first.save(update_fields=["to_date"])
    DeviceMapping.objects.create(device=device, branch=other_branch, from_date=moved_on)

    assert device.mappings.count() == 2
    assert device.open_mapping.from_date == moved_on
    # One open row, so "which station?" has exactly one answer.
    assert device.current_branch == other_branch
    # And the closed row still says where it used to be.
    assert device.mappings.filter(to_date__isnull=False).get().branch == branch


def test_a_mapping_may_not_end_before_it_starts(company, branch):
    device = make_device(company, status=DeviceStatus.APPROVED,
                         approved_at=datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC))

    with pytest.raises(IntegrityError):
        DeviceMapping.objects.create(
            device=device, branch=branch, from_date=TODAY,
            to_date=TODAY - datetime.timedelta(days=1),
        )


def test_a_device_carries_no_station_of_its_own(company, branch):
    """The station lives in DeviceMapping and nowhere else.

    An approved device need not have one: stations are Device Mapping's job,
    and a tablet without one simply cannot log in until it is mapped (client
    decision, 19 Sep 2026). The multi-device rule lives in the services, as it
    was never expressible in SQL.
    """
    device = make_device(company)

    assert not hasattr(device, "branch")
    assert device.current_branch is None

    DeviceMapping.objects.create(device=device, branch=branch, from_date=TODAY)

    assert device.current_branch == branch


def test_an_approved_device_records_when_it_was_approved(company):
    with pytest.raises(IntegrityError):
        make_device(company, status=DeviceStatus.APPROVED)


def test_deleting_the_approver_leaves_the_device_approved(company, django_user_model):
    """approved_by is deliberately absent from the constraint: naming it would
    make anyone who ever approved a device undeletable."""
    approver = django_user_model.objects.create_user("app.rover", "Byky#2026", company=company)
    device = make_device(
        company, status=DeviceStatus.APPROVED,
        approved_at=datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC), approved_by=approver,
    )

    with transaction.atomic():
        approver.delete()

    device.refresh_from_db()
    assert device.status == DeviceStatus.APPROVED
    assert device.approved_by_id is None


# -- device_for_installation: operator login's device lookup ------------------


def test_device_for_installation_finds_the_row(company):
    from apps.devices import services

    device = make_device(company)
    found = services.device_for_installation(device.installation_id)
    assert found.pk == device.pk


def test_device_for_installation_is_none_for_an_unregistered_one(company):
    from apps.devices import services

    assert services.device_for_installation("never-registered") is None

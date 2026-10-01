"""A device's receipt counter per station.

The format checks use the legacy's real numbers, taken from its order table:
DUBPP60182000335 is order 335 from device 60182 at a DUBPP station, and
TDUBPP60182000006 is test ride 6 from the same device.
"""

import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.devices import services
from apps.devices.models import BillContinuity, BillKind, Device, DeviceSettings, DeviceStatus
from core.enums import ApprovalStatus, Channel


@pytest.fixture
def device(company):
    """Registration id set explicitly to the legacy device's number, so the
    format can be checked against a real receipt."""
    return Device.objects.create(
        company=company, installation_id="dubai-tablet", platform="android",
        channel=Channel.OPERATOR, status=DeviceStatus.APPROVED, approved_at=timezone.now(),
        device_registration_id=60182,
    )


def counter(device, branch, *, kind=BillKind.ORDER, prefix="DUBPP", last=0):
    return BillContinuity.objects.create(
        device=device, branch=branch, kind=kind, prefix=prefix, last_number=last
    )


def test_one_counter_per_device_station_and_kind(device, branch):
    """The legacy had no such rule, and its data already holds two duplicate
    groups -- each would break its SELECT BillNumber with "more than one value"."""
    counter(device, branch)
    with pytest.raises(IntegrityError):
        counter(device, branch)


def test_orders_and_test_rides_count_separately(device, branch):
    counter(device, branch, kind=BillKind.ORDER)
    counter(device, branch, kind=BillKind.TEST_RIDE)  # no error

    assert BillContinuity.objects.filter(device=device).count() == 2


def test_a_new_station_starts_a_new_counter(device, branch, other_branch):
    """As in the legacy: moving a device starts at 0 at the new station and
    leaves the old counter as it was. Safe because each station has its own
    prefix."""
    counter(device, branch, last=335)
    counter(device, other_branch, prefix="MARIN")

    assert BillContinuity.objects.get(branch=branch).last_number == 335
    assert BillContinuity.objects.get(branch=other_branch).last_number == 0


def test_the_last_number_cannot_go_negative(device, branch):
    with pytest.raises(IntegrityError):
        counter(device, branch, last=-1)


def test_the_next_number_follows_the_last(device, branch):
    assert counter(device, branch, last=335).next_number == 336


def test_an_order_number_matches_the_legacy_receipt(device, branch):
    assert services.format_bill_number(counter(device, branch), 335) == "DUBPP60182000335"


def test_a_test_ride_number_matches_the_legacy_receipt(device, branch):
    ride = counter(device, branch, kind=BillKind.TEST_RIDE)
    assert services.format_bill_number(ride, 6) == "TDUBPP60182000006"


def test_the_default_number_is_the_next_one(device, branch):
    assert services.format_bill_number(counter(device, branch, last=335)) == "DUBPP60182000336"


def test_past_six_digits_the_number_grows(device, branch):
    assert services.format_bill_number(counter(device, branch), 1_234_567) == "DUBPP601821234567"


def test_the_prefix_is_a_snapshot(device, branch):
    """A counter keeps the format it started with, so a later change to the
    station's prefix cannot make this device's old and new numbers collide."""
    row = counter(device, branch, prefix="DUBPP")
    DeviceSettings.objects.create(
        branch=branch, settings_code="S01", order_no_prefix="NEWPX",
        header_1="BYKY", header_2="Corniche", footer_1="Thanks",
        approval_status=ApprovalStatus.APPROVED,
    )

    row.refresh_from_db()
    assert services.format_bill_number(row, 1).startswith("DUBPP")


def test_the_prefix_comes_from_approved_settings(branch):
    DeviceSettings.objects.create(
        branch=branch, settings_code="S01", order_no_prefix="dubpp",
        header_1="BYKY", header_2="Corniche", footer_1="Thanks",
        approval_status=ApprovalStatus.APPROVED,
    )
    assert services.bill_prefix_for(branch) == "DUBPP"


def test_pending_settings_do_not_set_the_prefix(branch):
    """Falls back to the branch code, as the legacy did with no prefix set."""
    DeviceSettings.objects.create(
        branch=branch, settings_code="S01", order_no_prefix="DUBPP",
        header_1="BYKY", header_2="Corniche", footer_1="Thanks",
    )
    assert services.bill_prefix_for(branch) == branch.short_code.upper()


def test_with_no_settings_the_branch_code_is_the_prefix(branch):
    assert services.bill_prefix_for(branch) == "AUH01"


# -- counter_for: operator login's get-or-create ------------------------------


def test_counter_for_creates_one_at_zero_the_first_time(device, branch):
    row = services.counter_for(device, branch, BillKind.ORDER)
    assert row.last_number == 0
    assert row.device == device and row.branch == branch and row.kind == BillKind.ORDER


def test_counter_for_returns_the_same_row_the_second_time(device, branch):
    first = services.counter_for(device, branch, BillKind.ORDER)
    first.last_number = 40
    first.save(update_fields=["last_number"])

    second = services.counter_for(device, branch, BillKind.ORDER)
    assert second.pk == first.pk
    assert second.last_number == 40


def test_counter_for_seeds_the_prefix_from_the_branch(device, branch):
    DeviceSettings.objects.create(
        branch=branch, settings_code="S01", order_no_prefix="dubpp",
        header_1="BYKY", header_2="Corniche", footer_1="Thanks",
        approval_status=ApprovalStatus.APPROVED,
    )
    row = services.counter_for(device, branch, BillKind.ORDER)
    assert row.prefix == "DUBPP"


def test_counter_for_order_and_test_ride_are_independent(device, branch):
    order = services.counter_for(device, branch, BillKind.ORDER)
    ride = services.counter_for(device, branch, BillKind.TEST_RIDE)
    assert order.pk != ride.pk
    assert BillContinuity.objects.filter(device=device, branch=branch).count() == 2


# -- Raised from an uploaded receipt -------------------------------------------


@pytest.mark.parametrize(("last", "receipt", "expected"), [
    (0, "DUBPP60182000335", 335),            # the number inside this device's own receipt
    (335, "DUBPP60182000335", 335),          # resent: no change
    (340, "DUBPP60182000335", 340),          # an older receipt, uploaded late: never backwards
    (0, "DUBPP601821234567", 1_234_567),     # past six digits
    (0, "DUBPP60183000335", 0),              # another device's head
    (0, "MARIN60182000335", 0),              # another prefix
    (0, "DUBPP60182", 0),                    # no number
    (0, "DUBPP6018233", 0),                  # too short to be one of ours
    (0, "DUBPP60182000A35", 0),              # not digits
], ids=["raised", "resent", "late", "seven-digits", "other-device", "other-prefix", "head-only", "short",
        "not-digits"])
def test_an_uploaded_receipt_raises_the_counter_to_its_number(device, branch, last, receipt, expected):
    counter(device, branch, last=last)

    raised = services.raise_counter_from(device, branch, BillKind.ORDER, receipt)

    assert raised.last_number == expected == BillContinuity.objects.get(device=device).last_number


def test_a_test_ride_receipt_raises_only_the_test_ride_counter(device, branch):
    counter(device, branch, kind=BillKind.ORDER)
    counter(device, branch, kind=BillKind.TEST_RIDE)

    services.raise_counter_from(device, branch, BillKind.TEST_RIDE, "TDUBPP60182000006")
    services.raise_counter_from(device, branch, BillKind.TEST_RIDE, "DUBPP60182000335")    # no T: not a ride

    assert dict(BillContinuity.objects.values_list("kind", "last_number")) == {
        BillKind.ORDER: 0, BillKind.TEST_RIDE: 6}

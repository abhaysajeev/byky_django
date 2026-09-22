"""Per-station tablet settings.

The legacy kept "one active settings row per station" only in its screen,
which hid stations that already had one. Here the database holds it.
"""

from decimal import Decimal

import pytest
from django.db import IntegrityError

from apps.company.models import DiscountType, TaxType
from apps.devices.models import DeviceSettings, PrintType, RoundOffMode
from core.enums import ApprovalStatus


def settings(branch, *, code="S01", prefix="DUBPP", active=True, **extra):
    fields = {
        "branch": branch, "settings_code": code, "order_no_prefix": prefix,
        "header_1": "BYKY", "header_2": "Corniche", "footer_1": "Thank you",
        "is_active": active,
    }
    fields.update(extra)
    return DeviceSettings.objects.create(**fields)


def test_a_station_has_one_active_settings_row(branch):
    settings(branch)
    with pytest.raises(IntegrityError):
        settings(branch, code="S02", prefix="OTHER")


def test_an_inactive_row_does_not_count(branch):
    settings(branch, active=False)
    settings(branch, code="S02", prefix="OTHER")  # no error

    assert DeviceSettings.objects.filter(branch=branch).count() == 2


def test_a_settings_code_is_unique_among_active_rows(branch, other_branch):
    settings(branch, code="S01")
    with pytest.raises(IntegrityError):
        settings(other_branch, code="S01", prefix="OTHER")


def test_an_order_prefix_belongs_to_one_station(branch, other_branch):
    """Two stations sharing a prefix could print the same receipt number."""
    settings(branch, prefix="DUBPP")
    with pytest.raises(IntegrityError):
        settings(other_branch, code="S02", prefix="DUBPP")


def test_the_prefix_is_stored_upper_case(branch, other_branch):
    """So "dubpp" and "DUBPP" count as the same prefix, as the legacy's
    upper-casing at print time implied."""
    row = settings(branch, prefix="dubpp")
    assert row.order_no_prefix == "DUBPP"

    with pytest.raises(IntegrityError):
        settings(other_branch, code="S02", prefix="DuBpP")


@pytest.mark.parametrize("field,value", [
    ("paper_feed", 11),
    ("receipt_copies", 0),
    ("receipt_copies", 11),
    ("customer_test_minutes", 0),
    ("cashier_test_minutes", 0),
])
def test_values_stay_in_their_legacy_ranges(branch, field, value):
    with pytest.raises(IntegrityError):
        settings(branch, **{field: value})


def test_defaults_are_what_the_live_rows_hold(branch):
    """The client's 95 live rows: paper feed 2 (60 of them; the other 35 are 4),
    landscape -- the only print type the legacy screen ever offered -- upward
    rounding to 25 fils, one receipt copy, five-minute test slots."""
    row = settings(branch)

    assert row.paper_feed == 2
    assert row.print_type == PrintType.LANDSCAPE
    assert row.round_off_mode == RoundOffMode.UPWARD
    assert row.round_off_step == Decimal("0.25")
    assert row.receipt_copies == 1
    assert (row.customer_test_minutes, row.cashier_test_minutes) == (5, 5)
    assert row.share_on_whatsapp is False


def test_settings_start_pending_like_every_master(branch):
    row = settings(branch)
    assert row.approval_status == ApprovalStatus.PENDING


def test_tax_and_discount_type_live_on_the_company(company):
    """The legacy copied them onto every settings row from the company, so they
    were only ever one value. Defaults are its tablet proc's ISNULL fallbacks."""
    assert company.tax_type == TaxType.INCLUDED
    assert company.discount_type == DiscountType.BEFORE_TAX
    assert not hasattr(DeviceSettings, "tax_type")


# -- Settings Code and Order Prefix are unique per company (20 Sep 2026) -----


def second_company(like):
    from apps.company.models import Company
    return Company.objects.create(
        short_code="TWO", name="Second", country=like.country, state=like.state,
        phone_number="+9712222222", email="two@byky.test",
    )


def test_the_company_is_copied_from_the_branch(branch):
    row = settings(branch)
    assert row.company_id == branch.company_id


def test_two_companies_may_use_the_same_settings_code_and_prefix(company, branch, other_branch):
    """A code is per company, not global -- a receipt is still unique either
    way, since the device's own registration id sits inside every printed
    number (design/03-login.md section 8.8)."""
    from apps.company.models import Branch, BranchType, Location

    other = second_company(company)
    theirs = Branch.objects.create(
        company=other,
        location=Location.objects.create(
            country=company.country, state=company.state, short_code="OTHLOC", name="Elsewhere",
        ),
        short_code="OTH01", name="Their Station", branch_type=BranchType.STATION,
    )

    ours = settings(branch, code="S01", prefix="DUBPP")
    theirs_row = settings(theirs, code="S01", prefix="DUBPP")

    assert (ours.settings_code, theirs_row.settings_code) == ("S01", "S01")
    assert (ours.order_no_prefix, theirs_row.order_no_prefix) == ("DUBPP", "DUBPP")


def test_one_company_still_cannot_reuse_its_own_settings_code(branch, other_branch):
    settings(branch, code="S01", prefix="AAA01")
    with pytest.raises(IntegrityError):
        settings(other_branch, code="S01", prefix="BBB01")


def test_one_company_still_cannot_reuse_its_own_prefix(branch, other_branch):
    settings(branch, code="S01", prefix="DUBPP")
    with pytest.raises(IntegrityError):
        settings(other_branch, code="S02", prefix="DUBPP")

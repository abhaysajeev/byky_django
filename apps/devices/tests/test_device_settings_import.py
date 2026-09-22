"""Importing the client's live device settings, logos included.

The one that matters most is `test_the_logo_arrives_byte_for_byte`: these
bitmaps are 1-bit and 832 dots wide because that is the print head's width, so
anything that re-encoded one on the way in would change what a station prints,
and nobody would notice until a receipt came out wrong.

Reads the real export in `data/legacy/`, as the company import's tests do.
"""

import base64
import datetime

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.company.models import Branch
from apps.devices import legacy_import as legacy
from apps.devices import services
from apps.devices.models import DeviceSettings, PrintType, RoundOffMode
from core.enums import ApprovalStatus

pytestmark = pytest.mark.django_db


# --- reading the export -------------------------------------------------------


def test_the_export_holds_every_live_station():
    rows = legacy.settings()
    assert len(rows) == 95
    assert len({pk for pk, *_ in rows}) == 95          # legacy ids, kept


def test_a_row_carries_what_the_receipt_prints():
    creek = next(d for pk, d, *_ in legacy.settings() if d["settings_code"] == "creek1")

    assert creek["header_1"] == "CREEK PARK 1"
    assert creek["header_2"] == "DUBAI"
    assert creek["additional_header_1"].startswith("BYKY SPORT")
    assert creek["order_no_prefix"] == "CF001"
    assert creek["branch_id"] == 1


def test_the_legacy_codes_become_the_words_they_meant():
    """PrintType 1 is Landscape -- the only one its enum has. RoundOff 1 is
    Upward, and RoundOffLimit 0 is the amount 0.25 (SFA.cs:910-914)."""
    rows = [d for _, d, *_ in legacy.settings()]

    assert {d["print_type"] for d in rows} == {PrintType.LANDSCAPE}
    assert {d["round_off_mode"] for d in rows} == {RoundOffMode.UPWARD, RoundOffMode.NEAREST}
    assert {str(d["round_off_step"]) for d in rows} == {"0.25"}


def test_a_missing_test_slot_falls_back_to_five_minutes():
    """The tablet proc's own ISNULL(TestTimeSlot, 5)."""
    rows = [d for _, d, *_ in legacy.settings()]
    assert all(d["customer_test_minutes"] > 0 and d["cashier_test_minutes"] > 0 for d in rows)


def test_whatsapp_starts_off_because_the_legacy_had_no_such_column():
    assert not any(d["share_on_whatsapp"] for _, d, *_ in legacy.settings())


def test_the_prefix_is_upper_case_as_it_prints():
    assert all(d["order_no_prefix"].isupper() for _, d, *_ in legacy.settings())


def test_settings_for_a_station_that_was_not_imported_are_reported_not_dropped():
    rows = legacy.settings(branch_ids={1})
    assert [d["settings_code"] for _, d, *_ in rows] == ["creek1"]
    assert len(legacy.skipped(branch_ids={1})) == 94
    assert all("not imported" in line for line in legacy.skipped(branch_ids={1}))


# --- the logos ----------------------------------------------------------------


def test_every_station_brought_its_logo():
    rows = legacy.settings()
    assert all(d["logo"] for _, d, *_ in rows)
    assert legacy.without_logo() == []


def test_the_logos_are_the_bitmaps_the_print_head_takes():
    """94 of the 95 are 1-bit and 832 dots wide -- the width of an 80 mm head."""
    shapes = set()
    for _, defaults, *_ in legacy.settings():
        raw = defaults["logo"]
        assert raw[:2] == b"BM"
        shapes.add((int.from_bytes(raw[18:22], "little"), int.from_bytes(raw[28:30], "little")))
    assert shapes == {(832, 1), (240, 1)}


def test_the_logo_arrives_byte_for_byte():
    """Nothing re-encodes it: the bytes in the row are the bytes in the file."""
    pk, defaults, *_ = next(
        (pk, d, *rest) for pk, d, *rest in legacy.settings() if d["settings_code"] == "creek1"
    )
    from apps.devices.legacy_import import LOGOS, load

    row = next(r for r in load() if r["SettingCode"] == "creek1")
    assert defaults["logo"] == (LOGOS / row["LogoFile"]).read_bytes()
    assert len(defaults["logo"]) == row["LogoBytes"]


# --- running the command ---------------------------------------------------------


@pytest.fixture
def legacy_branch(company, branch):
    """Creek Park 1, at the legacy's own BranchID.

    The export's rows point at legacy branch ids, which is the whole reason the
    company import preserves them; the shared `branch` fixture makes a station
    with a fresh key, so it would match nothing.
    """
    return Branch.objects.create(
        pk=1, company=company, location=branch.location,
        short_code="CREEK1", name="creek park 1", branch_type=branch.branch_type,
    )


@pytest.fixture
def imported(legacy_branch):
    """The export, loaded against the branches that exist in this test."""
    call_command("import_legacy_device_settings", verbosity=0)


def test_it_refuses_before_the_branches_exist(db):
    with pytest.raises(CommandError, match="import_legacy_branches"):
        call_command("import_legacy_device_settings", verbosity=0)


def test_rows_land_with_their_legacy_identity(legacy_branch, imported):
    """`branch` is the fixture's own station; only settings for stations that
    exist are written, so this one row proves the mapping."""
    row = DeviceSettings.objects.get(branch=legacy_branch)

    assert row.pk in {pk for pk, *_ in legacy.settings()}
    assert row.approval_status == ApprovalStatus.APPROVED
    assert row.is_active


def test_the_legacy_dates_are_kept_not_stamped_with_now(legacy_branch, imported):
    """A tablet re-downloads its logo only when logo_changed_on moves, so
    importing them as "now" would send every station to fetch one it has."""
    row = DeviceSettings.objects.get(branch=legacy_branch)

    assert row.created_on.year < datetime.date.today().year or row.created_on.date() < datetime.date.today()
    assert row.logo_changed_on is None or row.logo_changed_on < datetime.datetime.now(datetime.UTC)


def test_running_it_twice_changes_nothing(legacy_branch, imported):
    before = DeviceSettings.objects.get(branch=legacy_branch)
    call_command("import_legacy_device_settings", verbosity=0)
    after = DeviceSettings.objects.get(branch=legacy_branch)

    assert DeviceSettings.objects.count() == 1
    assert (after.pk, after.settings_code, bytes(
        DeviceSettings.objects.with_logo().get(pk=after.pk).logo
    )) == (before.pk, before.settings_code, bytes(
        DeviceSettings.objects.with_logo().get(pk=before.pk).logo
    ))


def test_what_the_tablet_is_handed_is_what_the_legacy_sent(legacy_branch, imported):
    """The whole pipeline: the legacy's base64 -> bytes in the row -> base64
    again in the payload, unchanged, so a station prints the same receipt."""
    row = DeviceSettings.objects.with_logo().get(branch=legacy_branch)

    payload = services.settings_payload(legacy_branch)

    assert base64.b64decode(payload["logo"]) == bytes(row.logo)
    assert payload["order_no_prefix"] == row.order_no_prefix
    assert payload["header_1"] == row.header_1
    assert services.bill_prefix_for(legacy_branch) == row.order_no_prefix


def test_a_station_can_still_be_given_settings_by_hand_afterwards(legacy_branch, imported, company, branch):
    """The import writes the legacy's own ids, which leaves Postgres's counter
    behind: the next row added from a screen would fail with "duplicate key".
    Invisible until someone adds a station, which is the worst time to find it
    (apps/company/legacy_import.reset_sequence)."""
    imported_max = DeviceSettings.objects.order_by("-pk").first().pk

    row = DeviceSettings.objects.create(
        branch=branch, settings_code="NEW1", order_no_prefix="NEW01",
        header_1="NEW STATION", header_2="DUBAI", footer_1="Thank you",
    )

    assert row.pk > imported_max

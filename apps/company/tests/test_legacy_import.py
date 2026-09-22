"""Importing the client's live Country and State rows.

The one that matters most is `test_a_displaced_id_moves_its_references_first`.
`State 2` is Abu Dhabi in the database we built and Sharjah in the legacy, so a
plain write by primary key would leave the company's registered address reading
Sharjah without anything saying so. That is the failure this import exists to
prevent, and it is silent if it ever comes back.
"""

import datetime

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.company import legacy_import as legacy
from apps.company.models import Company, Country, State
from core.enums import ApprovalStatus

# --- reading the export -------------------------------------------------------

def test_kuwait_is_left_out():
    """UAE only for the pilot, and the row is reported rather than dropped."""
    assert [pk for pk, *_ in legacy.countries()] == [1]
    assert 8 not in [pk for pk, *_ in legacy.states()]          # Mangaf
    assert any("KUWAIT" in line for line in legacy.skipped())
    assert any("MANGAF" in line for line in legacy.skipped())


def test_codes_are_imported_verbatim():
    """971 is a dialling code and a state's code is its own id. Both are what
    the legacy holds; neither is corrected here."""
    (_, country, *_), = legacy.countries()
    assert country["short_code"] == "971"
    assert country["name"] == "UAE"

    dubai = next(defaults for pk, defaults, *_ in legacy.states() if pk == 1)
    assert dubai["short_code"] == "1"
    assert dubai["name"] == "DUBAI"


def test_every_state_belongs_to_the_imported_country():
    assert {d["country_id"] for _, d, *_ in legacy.states()} == {1}


def test_approval_flags_become_the_three_fields():
    (_, country, *_), = legacy.countries()
    assert country["approval_status"] == ApprovalStatus.APPROVED
    assert country["is_active"] is True
    # No legacy source: a row that is already live needs no second pair of eyes.
    assert country["want_approval"] is False


def test_legacy_local_time_is_read_as_gulf_time():
    """The export holds naive wall-clock time from a system running in the UAE."""
    assert legacy.moment("2017-01-25 14:51:33.743") == datetime.datetime(
        2017, 1, 25, 10, 51, 33, 743000, tzinfo=datetime.UTC
    )
    assert legacy.moment(None) is None


def test_an_unreadable_timestamp_is_refused_rather_than_guessed():
    with pytest.raises(ValueError):
        legacy.moment("25/01/2017")


# --- the command --------------------------------------------------------------

@pytest.fixture
def imported(db):
    call_command("import_legacy_geography", verbosity=0)


def test_the_import_writes_the_live_rows(imported):
    assert Country.objects.count() == 1
    assert State.objects.count() == 7
    assert Country.objects.get(pk=1).name == "UAE"
    assert State.objects.get(pk=2).name == "SHARJAH"


def test_legacy_ids_are_preserved(imported):
    """CoreLocation.StateID and CoreBranch.LocationID still point at the right
    rows when those tables are imported."""
    assert dict(State.objects.values_list("pk", "name")) == {
        1: "DUBAI", 2: "SHARJAH", 3: "ABUDHABI", 4: "FUJAIRAH",
        5: "RAK", 6: "AJMAN", 7: "ALAIN",
    }


def test_the_real_creation_dates_survive(imported):
    """TimeStampedModel would otherwise stamp every row with the import time."""
    assert Country.objects.get(pk=1).created_on.year == 2017
    assert all(state.created_on.year == 2017 for state in State.objects.all())


def test_running_it_twice_changes_nothing(imported):
    before = list(Country.objects.values()) + list(State.objects.values())
    call_command("import_legacy_geography", verbosity=0)
    assert list(Country.objects.values()) + list(State.objects.values()) == before


# --- the stand-in rows --------------------------------------------------------

@pytest.fixture
def stand_ins(db):
    """The database as the build left it: a hand-made UAE and Abu Dhabi, with
    the company's registered address pointing at them."""
    country = Country.objects.create(pk=2, short_code="AE", name="United Arab Emirates")
    state = State.objects.create(pk=2, country=country, short_code="AUH", name="Abu Dhabi")
    return Company.objects.create(
        short_code="BYKY", name="BYKY", country=country, state=state,
        phone_number="+9710000000", email="ops@byky.test",
    )


def test_a_stand_in_stops_the_import_until_it_is_asked_about(stand_ins):
    with pytest.raises(CommandError):
        call_command("import_legacy_geography", verbosity=0)

    assert Country.objects.get(pk=2).short_code == "AE"      # nothing written
    assert not Country.objects.filter(pk=1).exists()


def test_a_displaced_id_moves_its_references_first(stand_ins):
    """State 2 is Abu Dhabi here and Sharjah in the legacy.

    Writing the import over it would silently move the company to Sharjah. The
    company must end up on the real Abu Dhabi instead.
    """
    call_command("import_legacy_geography", "--replace-stand-ins", verbosity=0)

    stand_ins.refresh_from_db()
    assert State.objects.get(pk=2).name == "SHARJAH"
    assert stand_ins.state_id == 3
    assert State.objects.get(pk=3).name == "ABUDHABI"


def test_an_orphan_stand_in_is_moved_then_deleted(stand_ins):
    call_command("import_legacy_geography", "--replace-stand-ins", verbosity=0)

    stand_ins.refresh_from_db()
    assert not Country.objects.filter(short_code="AE").exists()
    assert stand_ins.country_id == 1
    assert Country.objects.get(pk=1).short_code == "971"


def test_the_id_counter_moves_past_the_imported_rows(db):
    """Branches and locations are written with the legacy's own ids. Postgres's
    counter only moves when it supplies an id, so without a reset the next
    branch added from a screen collides with an imported one."""
    from apps.company.models import Branch, BranchType, Location

    call_command("import_legacy_geography", verbosity=0)
    call_command("import_legacy_company", verbosity=0)
    call_command("import_legacy_branches", verbosity=0)

    location = Location.objects.first()
    branch = Branch.objects.create(
        company_id=1, location=location, short_code="NEW01", name="New Station",
        branch_type=BranchType.STATION,
    )

    assert branch.pk > Branch.objects.exclude(pk=branch.pk).order_by("-pk").first().pk - 1


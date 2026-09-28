"""prune_unplaced_vehicles: a company's vehicles with no branch go, every
identifier left is renumbered gap-free, and a dry run touches nothing --
not even the identifier sequence, which Postgres never rolls back."""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.fare.tests import test_api as fare_api
from apps.fleet.models import UOM, Vehicle
from core.enums import ApprovalStatus

world = fare_api.world


@pytest.fixture
def fleet(world):
    """Ours: four at ADC1 (names out of order), three at no branch. Theirs: one, at no branch."""
    uom = UOM.objects.create(company=world["company"], uom_code="NO", uom_name="Number")
    their_uom = UOM.objects.create(company=world["other"], uom_code="NO", uom_name="Number")

    def make(name, vehicle_type, branch, company_uom=uom, **fields):
        return Vehicle.objects.create(company=vehicle_type.company, vehicle_code=name.replace(" ", ""),
                                      vehicle_name=name, vehicle_type=vehicle_type, uom=company_uom,
                                      current_branch=branch, approval_status=ApprovalStatus.APPROVED, **fields)

    for name in ("MO 10", "SPARE 1", "MO 2", "SPARE 2"):
        make(name, world["monaco"], None if name.startswith("SPARE") else world["adc1"])
    make("BE 1", world["berg"], world["adc1"])
    make("MO 1", world["monaco"], world["adc2"])
    make("OLD 1", world["berg"], None, is_active=False)
    make("KART 1", world["their_type"], None, company_uom=their_uom)


def prune(*extra):
    call_command("prune_unplaced_vehicles", "--company", "0598", *extra)


def listing():
    return list(Vehicle.objects.order_by("identifier_no").values_list("identifier", "vehicle_name"))


def test_a_dry_run_writes_nothing_and_leaves_the_sequence_alone(fleet, world, capsys):
    before = listing()
    prune()
    assert listing() == before
    report = capsys.readouterr().out
    assert "3 vehicle(s) with no branch to delete (2 marked active, 1 inactive); 4 at a branch kept" in report
    assert "5 vehicle(s), VB0001 to VB0005" in report and "Dry run" in report
    # The next vehicle still takes the number after the existing ones.
    last = max(Vehicle.objects.values_list("identifier_no", flat=True))
    newest = Vehicle.objects.create(company=world["company"], vehicle_code="NEW", vehicle_name="NEW",
                                    vehicle_type=world["monaco"], uom=Vehicle.objects.first().uom)
    assert newest.identifier_no == last + 1


def test_commit_deletes_this_companys_unplaced_vehicles_and_renumbers_gap_free(fleet, world):
    prune("--commit")
    # Ours at no branch are gone, active or not; another company's are not touched.
    assert not Vehicle.objects.filter(company=world["company"], current_branch__isnull=True).exists()
    assert Vehicle.objects.filter(vehicle_name="KART 1").exists()
    # Every vehicle left, numbered 1..n: category, type (Berg < Kart < Monaco), then number-aware name.
    assert listing() == [("VB0001", "BE 1"), ("VB0002", "KART 1"), ("VB0003", "MO 1"),
                         ("VB0004", "MO 2"), ("VB0005", "MO 10")]
    # The sequence carries on from the last number.
    fresh = Vehicle.objects.create(company=world["company"], vehicle_code="NEW", vehicle_name="NEW",
                                   vehicle_type=world["monaco"], uom=Vehicle.objects.first().uom,
                                   current_branch=world["adc1"])
    fresh.refresh_from_db()
    assert fresh.identifier == "VB0006"


def test_an_unknown_company_is_refused(fleet):
    with pytest.raises(CommandError):
        call_command("prune_unplaced_vehicles", "--company", "NOPE", "--commit")

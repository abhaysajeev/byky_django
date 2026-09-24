"""apps.fare.services: saving a whole fare, and the form's live check."""

from decimal import Decimal

import pytest

from apps.company.models import WeekDay
from apps.fare import payload, services
from apps.fare.models import Fare, FareBranch, FareLevel, FareRule, FareSeason
from apps.fare.services import Invalid, Stale
from apps.fare.tests.conftest import D, make_fare
from core.enums import ApprovalStatus

SAT, SUN = WeekDay.SATURDAY, WeekDay.SUNDAY


def price(fare, conc=10):
    return {"base_fare": str(fare), "grace_minutes": 5, "concurrent_interval_minutes": 10,
            "concurrent_fare": str(conc), "concurrent_grace_minutes": 0}


def every(key, start, end, fare, **extra):
    return {"key": key, "kind": "every_day", "start": start, "end": end, "price": price(fare), **extra}


def days(key, weekdays, start, end, fare, **extra):
    return {"key": key, "kind": "selected_days", "weekdays": list(weekdays), "start": start, "end": end,
            "price": price(fare), **extra}


def single(key, on, start, end, fare, **extra):
    return {"key": key, "kind": "single_date", "on_date": on, "start": start, "end": end,
            "price": price(fare), **extra}


def data(world, **changes):
    values = {"vehicle_type": world["monaco"].pk, "level": "company", "branches": [],
              "valid_from": "2026-01-01", "valid_to": "2026-12-31", "is_active": True,
              "package_minutes": 30, "base": price(50), "rules": [], "seasons": []}
    values.update(changes)
    return values


def fields(error):
    return [e["field"] for e in error.value.errors]


def messages(error):
    return " ".join(e["message"] for e in error.value.errors)


# -- Create -----------------------------------------------------------------------


def test_a_company_fare_is_saved_approved_for_the_users_company(world, user):
    fare, warnings = services.save_fare(user, data(world, company=world["other"].pk, rules=[
        every("n1", "16:00", "20:00", 70), single("n2", "2026-12-02", "12:00", "00:00", 100),
    ]))
    assert fare.company == world["company"]                     # from the user, not the payload
    assert (fare.level, fare.is_active, fare.approval_status) == (FareLevel.COMPANY, True, ApprovalStatus.APPROVED)
    assert fare.created_by == user and fare.lock_version == 0
    assert fare.rules.get(kind="single_date").end_minute == 1440   # "00:00" as an end: midnight
    assert warnings == {"never": {}, "holidays": []}


def test_a_branch_fare_links_its_branches(world, user):
    fare, _ = services.save_fare(user, data(world, level="branch",
                                            branches=[world["adc1"].pk, world["adc2"].pk]))
    links = FareBranch.objects.filter(fare=fare)
    assert {link.branch for link in links} == {world["adc1"], world["adc2"]}
    assert {(link.valid_to, link.package_minutes) for link in links} == {(D(2026, 12, 31), 30)}


def test_an_inactive_fare_stays_inactive(world, user):
    fare, _ = services.save_fare(user, data(world, is_active=False))
    assert fare.is_active is False


# -- Update -----------------------------------------------------------------------


def saved_with_children(world, user):
    fare, _ = services.save_fare(user, data(world, rules=[every("n1", "16:00", "20:00", 70),
                                                          every("n2", "08:00", "10:00", 45)],
                                            seasons=[{"key": "n3", "name": "Spring", "start_date": "2026-04-10",
                                                      "end_date": "2026-05-15", "base": price(55),
                                                      "rules": [days("n4", [SAT, SUN], "14:00", "22:00", 85)]}]))
    return fare


def test_an_update_keeps_ids_and_drops_what_was_removed(world, user):
    fare = saved_with_children(world, user)
    sent = payload.serialise(services.with_children(Fare.objects.filter(pk=fare.pk)).get())
    evening = next(r for r in sent["rules"] if r["start"] == "16:00")
    evening["price"]["base_fare"] = "75.00"
    sent["rules"] = [evening]                                     # the morning price is removed
    sent["seasons"][0]["name"] = "Spring 2026"

    updated, _ = services.save_fare(user, sent)
    assert updated.lock_version == 1
    assert FareRule.objects.get(pk=evening["id"]).base_fare == Decimal("75.00")
    assert FareRule.objects.filter(fare=fare, season=None).count() == 1
    assert FareSeason.objects.get(pk=sent["seasons"][0]["id"]).name == "Spring 2026"


def test_two_rules_can_swap_their_windows(world, user):
    fare = saved_with_children(world, user)
    sent = payload.serialise(services.with_children(Fare.objects.filter(pk=fare.pk)).get())
    a, b = sent["rules"]
    a["start"], a["end"], b["start"], b["end"] = b["start"], b["end"], a["start"], a["end"]
    services.save_fare(user, sent)                               # deferred constraints: no error
    assert sorted(FareRule.objects.filter(fare=fare, season=None).values_list("start_minute", flat=True)) == [480, 960]


def test_a_stale_copy_is_refused(world, user):
    fare = saved_with_children(world, user)
    sent = payload.serialise(services.with_children(Fare.objects.filter(pk=fare.pk)).get())
    services.save_fare(user, sent)                               # someone saves first
    with pytest.raises(Stale) as stale:
        services.save_fare(user, sent)
    assert "Sara K saved this fare" in str(stale.value)


def test_switching_level_drops_or_adds_branches(world, user):
    fare, _ = services.save_fare(user, data(world, level="branch", branches=[world["adc1"].pk]))
    sent = payload.serialise(services.with_children(Fare.objects.filter(pk=fare.pk)).get())
    sent["level"] = "company"
    services.save_fare(user, sent)
    assert not FareBranch.objects.filter(fare=fare).exists()
    assert Fare.objects.get(pk=fare.pk).level == FareLevel.COMPANY


def test_moving_the_dates_moves_the_branch_copies(world, user):
    fare, _ = services.save_fare(user, data(world, level="branch", branches=[world["adc1"].pk]))
    sent = payload.serialise(services.with_children(Fare.objects.filter(pk=fare.pk)).get())
    sent["valid_to"] = "2026-06-30"
    services.save_fare(user, sent)
    assert FareBranch.objects.get(fare=fare).valid_to == D(2026, 6, 30)


# -- Refused ---------------------------------------------------------------------------


def test_another_companys_vehicle_type_or_branch_is_refused(world, user):
    with pytest.raises(Invalid) as error:
        services.save_fare(user, data(world, vehicle_type=world["their_type"].pk))
    assert fields(error) == ["Vehicle type"]
    with pytest.raises(Invalid) as error:
        services.save_fare(user, data(world, level="branch", branches=[world["theirs"].pk]))
    assert fields(error) == ["Branches"]


def test_an_inactive_vehicle_type_is_refused_for_a_new_fare(world, user):
    world["berg"].is_active = False
    world["berg"].save()
    with pytest.raises(Invalid):
        services.save_fare(user, data(world, vehicle_type=world["berg"].pk))


def test_a_row_of_another_fare_cannot_be_reached(world, user):
    theirs = saved_with_children(world, user)
    their_rule = FareRule.objects.filter(fare=theirs).first()
    with pytest.raises(Invalid) as error:
        services.save_fare(user, data(world, vehicle_type=world["berg"].pk,
                                      rules=[every("x", "16:00", "20:00", 70, id=their_rule.pk)]))
    assert "could not be matched" in messages(error)


def test_every_problem_is_reported_at_once(world, user):
    with pytest.raises(Invalid) as error:
        services.save_fare(user, data(world, package_minutes="thirty", base=price("-1"), rules=[
            every("n1", "16:00", "20:00", 70), every("n2", "18:00", "22:00", 80),
            every("n3", "25:00", "26:00", 1),
        ]))
    assert len(error.value.errors) >= 2
    assert {"Package time", "Special pricing · Every day · From"} <= set(fields(error))


def test_clashes_with_other_fares_are_named(world, user):
    services.save_fare(user, data(world))
    with pytest.raises(Invalid) as error:
        services.save_fare(user, data(world, valid_from="2026-06-01", valid_to="2027-05-31"))
    assert "An active company fare for Monaco (30 min) already covers 1 Jan 2026 – 31 Dec 2026" in messages(error)

    services.save_fare(user, data(world, level="branch", branches=[world["adc1"].pk]))
    with pytest.raises(Invalid) as error:
        services.save_fare(user, data(world, level="branch", branches=[world["adc1"].pk, world["family"].pk]))
    assert messages(error).startswith("Abu Dhabi Corniche 1 already has an active fare for Monaco (30 min)")


def test_an_inactive_fare_clashes_with_nothing(world, user):
    services.save_fare(user, data(world))
    services.save_fare(user, data(world, is_active=False))


# -- Warnings and the live check ----------------------------------------------------------


def test_a_company_holiday_names_the_branches_it_will_not_reach(world, user):
    make_fare(world, branches=[world["adc1"]])
    _, warnings = services.save_fare(user, data(world, rules=[single("n1", "2026-12-02", "12:00", "22:00", 100)]))
    assert warnings["holidays"][0]["branches"] == ["Abu Dhabi Corniche 1"]
    assert warnings["holidays"][0]["message"].startswith("2 Dec 2026 won't reach Abu Dhabi Corniche 1")


def test_never_applies_comes_back_with_the_save(world, user):
    _, warnings = services.save_fare(user, data(
        world, rules=[every("n1", "16:00", "20:00", 70)],
        seasons=[{"key": "n2", "name": "All year", "start_date": "2026-01-01", "end_date": "2026-12-31",
                  "base": price(60), "rules": []}]))
    assert list(warnings["never"].values()) == [
        "Every day it could apply is inside a season, which uses its own prices."]


def test_the_check_answers_test_fare_without_saving(world, user):
    sent = data(world, rules=[days("n1", [SAT, SUN], "14:00", "22:00", 80)])
    answer = services.check(user, sent, {"date": "2026-09-26", "time": "15:00"})   # a Saturday
    assert answer["errors"] == []
    assert answer["test"]["price"]["base_fare"] == "80.00"
    assert answer["test"]["source"]["label"] == "Selected days · Sun, Sat · 14:00 – 22:00"
    assert [s["from"] for s in answer["test"]["schedule"]] == ["00:00", "14:00", "22:00"]
    assert not Fare.objects.exists()

    outside = services.check(user, sent, {"date": "2027-01-05", "time": "10:00"})
    assert outside["test"]["found"] is False


def test_the_check_stops_at_an_inconsistent_fare(world, user):
    answer = services.check(user, data(world, rules=[every("a", "16:00", "20:00", 70),
                                                     every("b", "18:00", "22:00", 80)]),
                            {"date": "2026-09-26", "time": "19:00"})
    assert answer["errors"][0]["key"] == "b"
    assert answer["test"] is None


def test_a_saved_fare_round_trips_through_the_form(world, user):
    fare = saved_with_children(world, user)
    loaded = services.with_children(Fare.objects.filter(pk=fare.pk)).get()
    sent = payload.serialise(loaded)
    parsed = payload.parse(sent)
    assert parsed.errors == []
    assert parsed.spec == services.spec_from_fare(loaded)

"""Branch Working Time: the rule for "is this branch open", the shift checks,
and the screen's three endpoints (load, save, assign to many).

The legacy held one window per branch per day and never crossed midnight
(0 of 560 RmsWorkingHours rows do); "open all day" was 00:00-23:59.
"""

import datetime
import zoneinfo

import pytest

from apps.company import services
from apps.company.models import Branch, BranchType, BranchWorkingTime, WeekDay
from apps.company.tests.test_crud import client_in, post, world  # noqa: F401 -- fixtures

DUBAI = zoneinfo.ZoneInfo("Asia/Dubai")

# 2026-09-20 is a Sunday; the 21st a Monday.
SUNDAY = datetime.date(2026, 9, 20)


def at(day, hh, mm, zone=DUBAI):
    return datetime.datetime.combine(day, datetime.time(hh, mm), tzinfo=zone)


@pytest.fixture
def station(world):
    return Branch.objects.create(
        company=world["company"], location=world["location"], short_code="AUH01",
        name="Corniche Station", branch_type=BranchType.STATION,
    )


def shift(branch, day, number, start, end):
    return BranchWorkingTime.objects.create(
        branch=branch, week_day=day, shift_number=number, start_time=start, end_time=end,
    )


def week(*shifts):
    return [{"week_day": d, "shift_number": n, "start": s, "end": e} for d, n, s, e in shifts]


# -- Is the branch open? -------------------------------------------------------


def test_no_hours_at_all_is_not_set(station):
    assert services.branch_open_state(station, at(SUNDAY, 10, 0)) == services.NOT_SET


def test_inside_a_shift_is_open(station):
    shift(station, WeekDay.SUNDAY, 1, "08:00", "16:00")
    assert services.branch_open_state(station, at(SUNDAY, 10, 0)) == services.OPEN


def test_both_ends_of_a_shift_count_as_open(station):
    shift(station, WeekDay.SUNDAY, 1, "08:00", "16:00")
    assert services.branch_open_state(station, at(SUNDAY, 8, 0)) == services.OPEN
    assert services.branch_open_state(station, at(SUNDAY, 16, 0)) == services.OPEN
    assert services.branch_open_state(station, at(SUNDAY, 16, 1)) == services.CLOSED


def test_all_day_covers_the_last_minute(station):
    shift(station, WeekDay.SUNDAY, 1, "00:00", "23:59")
    moment = datetime.datetime(2026, 9, 20, 23, 59, 45, tzinfo=DUBAI)
    assert services.branch_open_state(station, moment) == services.OPEN


def test_between_two_shifts_is_closed(station):
    shift(station, WeekDay.SUNDAY, 1, "08:00", "12:00")
    shift(station, WeekDay.SUNDAY, 2, "14:00", "20:00")
    assert services.branch_open_state(station, at(SUNDAY, 13, 0)) == services.CLOSED
    assert services.branch_open_state(station, at(SUNDAY, 15, 0)) == services.OPEN


def test_a_day_with_no_shift_is_closed(station):
    shift(station, WeekDay.SUNDAY, 1, "08:00", "16:00")      # Sunday only
    monday = SUNDAY + datetime.timedelta(days=1)
    assert services.branch_open_state(station, at(monday, 10, 0)) == services.CLOSED


def test_the_company_timezone_decides_the_clock(station):
    """10:00 Dubai is 06:00 UTC. Read as UTC, the branch would look closed."""
    shift(station, WeekDay.SUNDAY, 1, "08:00", "16:00")
    utc_moment = datetime.datetime(2026, 9, 20, 6, 0, tzinfo=datetime.UTC)
    assert services.branch_open_state(station, utc_moment) == services.OPEN


def test_the_weekday_follows_the_local_date(station):
    """Sunday 02:00 in Dubai is still Saturday in UTC."""
    shift(station, WeekDay.SUNDAY, 1, "00:00", "03:00")
    utc_moment = datetime.datetime(2026, 9, 19, 22, 0, tzinfo=datetime.UTC)
    assert services.branch_open_state(station, utc_moment) == services.OPEN


# -- The shift rules -----------------------------------------------------------


def errors_for(*shifts):
    return services.validate_schedule(week(*shifts))[1]


def test_a_good_week_passes():
    rows, errors = services.validate_schedule(week(
        (WeekDay.SUNDAY, 1, "08:00", "12:00"), (WeekDay.SUNDAY, 2, "13:00", "20:00"), (WeekDay.MONDAY, 1, "00:00", "23:59"),
    ))
    assert errors == []
    assert len(rows) == 3


def test_empty_shifts_are_simply_not_worked():
    rows, errors = services.validate_schedule(week((WeekDay.SUNDAY, 1, "", ""), (WeekDay.MONDAY, 1, "08:00", "16:00")))
    assert errors == [] and len(rows) == 1


@pytest.mark.parametrize("start,end", [("16:00", "08:00"), ("08:00", "08:00"), ("04:00", "03:59")])
def test_end_must_be_after_start(start, end):
    """04:00-03:59 was the wireframe's default; it would cross midnight."""
    errors = errors_for((WeekDay.MONDAY, 1, start, end))
    assert errors[0]["field"] == "Monday shift 1"
    assert "after start" in errors[0]["message"]


def test_shifts_are_added_in_order():
    errors = errors_for((WeekDay.TUESDAY, 2, "13:00", "20:00"))
    assert errors == [{
        "field": "Tuesday shift 2", "message": "Fill shift 1 first; shifts are added in order.",
    }]


def test_a_shift_must_start_after_the_previous_one_ends():
    errors = errors_for((WeekDay.WEDNESDAY, 1, "08:00", "14:00"), (WeekDay.WEDNESDAY, 2, "13:00", "20:00"))
    assert errors[0]["field"] == "Wednesday shift 2"
    assert "14:00" in errors[0]["message"]


def test_touching_shifts_overlap():
    """Both ends are inclusive, so 12:00 cannot close one shift and open the next."""
    assert errors_for((WeekDay.WEDNESDAY, 1, "08:00", "12:00"), (WeekDay.WEDNESDAY, 2, "12:00", "20:00"))


def test_half_a_shift_is_refused():
    assert errors_for((WeekDay.SUNDAY, 1, "08:00", ""))[0]["message"] == "Enter both a start and an end time."


@pytest.mark.parametrize("bad", [
    {"week_day": 9, "shift_number": 1, "start": "08:00", "end": "10:00"},
    {"week_day": "x", "shift_number": 1, "start": "08:00", "end": "10:00"},
    {"week_day": 0, "shift_number": 5, "start": "08:00", "end": "10:00"},
    {"week_day": 0, "shift_number": 1, "start": "8am", "end": "10:00"},
    "not a shift",
])
def test_unreadable_shifts_are_errors_not_crashes(bad):
    assert services.validate_schedule([bad])[1]


def test_a_week_that_is_not_a_list_is_refused():
    assert services.validate_schedule({"week_day": 0})[1]


# -- Endpoints -------------------------------------------------------------------


def test_the_schedule_loads_as_json(client_in, station):
    shift(station, WeekDay.MONDAY, 1, "08:00", "16:00")

    body = client_in.get(f"/company/branch-working-time/schedule/{station.pk}/").json()

    assert body["ok"] is True
    assert body["branch"] == {"id": station.pk, "name": "Corniche Station"}
    assert body["shifts"] == [{"week_day": WeekDay.MONDAY, "shift_number": 1, "start": "08:00", "end": "16:00"}]


def test_another_companys_schedule_is_not_found(client_in, world):
    theirs = Branch.objects.create(
        company=world["other"], location=world["location"], short_code="OTH01",
        name="Their Station", branch_type=BranchType.STATION,
    )
    response = client_in.get(f"/company/branch-working-time/schedule/{theirs.pk}/")
    assert response.status_code == 404


def test_saving_returns_the_saved_week_and_it_reloads(client_in, station):
    """The bug the screen had: a save that seemed to vanish on refresh."""
    response = post(client_in, "/company/branch-working-time/save/", {
        "branch": station.pk,
        "shifts": week((WeekDay.MONDAY, 1, "09:15", "17:45"), (WeekDay.MONDAY, 2, "18:00", "22:00")),
    })
    assert response.json()["ok"] is True
    assert len(response.json()["shifts"]) == 2

    reloaded = client_in.get(f"/company/branch-working-time/schedule/{station.pk}/").json()
    assert reloaded["shifts"][0] == {"week_day": WeekDay.MONDAY, "shift_number": 1, "start": "09:15", "end": "17:45"}


def test_an_invalid_week_saves_nothing(client_in, station):
    shift(station, WeekDay.FRIDAY, 1, "09:00", "17:00")

    response = post(client_in, "/company/branch-working-time/save/", {
        "branch": station.pk, "shifts": week((WeekDay.SUNDAY, 1, "16:00", "08:00")),
    })

    assert response.status_code == 400
    assert response.json()["errors"][0]["field"] == "Sunday shift 1"
    assert BranchWorkingTime.objects.get(branch=station).week_day == WeekDay.FRIDAY   # old week untouched


def test_a_week_with_bad_values_is_400_not_500(client_in, station):
    response = post(client_in, "/company/branch-working-time/save/", {
        "branch": station.pk, "shifts": [{"week_day": "x", "shift_number": None}],
    })
    assert response.status_code == 400


def _branches(world, n):
    return [
        Branch.objects.create(
            company=world["company"], location=world["location"], short_code=f"B{i:02d}",
            name=f"Branch {i}", branch_type=BranchType.STATION,
        )
        for i in range(n)
    ]


def test_assign_copies_the_week_to_every_picked_branch(client_in, world):
    picked = _branches(world, 3)
    shift(picked[0], WeekDay.SATURDAY, 1, "10:00", "11:00")        # replaced, not merged

    response = post(client_in, "/company/branch-working-time/assign/", {
        "branches": [b.pk for b in picked],
        "shifts": week((WeekDay.SUNDAY, 1, "08:00", "16:00"), (WeekDay.MONDAY, 1, "08:00", "16:00")),
    })

    body = response.json()
    assert body["ok"] is True
    assert body["message"] == "Schedule assigned to 3 branches."
    for branch in picked:
        assert sorted(BranchWorkingTime.objects.filter(branch=branch)
                      .values_list("week_day", flat=True)) == sorted([WeekDay.SUNDAY, WeekDay.MONDAY])


def test_assign_with_a_foreign_branch_writes_nothing(client_in, world):
    ours = _branches(world, 1)[0]
    theirs = Branch.objects.create(
        company=world["other"], location=world["location"], short_code="OTH01",
        name="Their Station", branch_type=BranchType.STATION,
    )

    response = post(client_in, "/company/branch-working-time/assign/", {
        "branches": [ours.pk, theirs.pk], "shifts": week((WeekDay.SUNDAY, 1, "08:00", "16:00")),
    })

    assert response.status_code == 400
    assert BranchWorkingTime.objects.count() == 0


def test_assign_needs_a_branch(client_in):
    response = post(client_in, "/company/branch-working-time/assign/", {
        "branches": [], "shifts": week((WeekDay.SUNDAY, 1, "08:00", "16:00")),
    })
    assert response.status_code == 400


def test_assign_refuses_an_invalid_week(client_in, world):
    picked = _branches(world, 2)
    response = post(client_in, "/company/branch-working-time/assign/", {
        "branches": [b.pk for b in picked], "shifts": week((WeekDay.SUNDAY, 2, "08:00", "16:00")),
    })
    assert response.status_code == 400
    assert BranchWorkingTime.objects.count() == 0


# -- The page ------------------------------------------------------------------------


def test_the_page_shows_day_names_and_no_invented_times(client_in, station):
    html = client_in.get("/company/branch-working-time/edit/").content.decode()

    for day in ("Sunday", "Monday", "Saturday"):
        assert f'<td class="scr-wt-day">{day}</td>' in html
    assert "&#x27;Sunday&#x27;)" not in html and "'Sunday')" not in html
    assert "04:00" not in html and "03:59" not in html


def test_the_page_lists_the_uae_week_sunday_first(client_in, station):
    """Stored numbering is Python's (Monday 0); the screen keeps the UAE week."""
    html = client_in.get("/company/branch-working-time/edit/").content.decode()
    order = [html.index(f'<td class="scr-wt-day">{day}</td>') for day in
             ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")]
    assert order == sorted(order)
    assert f'data-day="{WeekDay.SUNDAY.value}" data-shift="1" data-label="Sunday shift 1"' in html


def test_week_day_of_is_pythons_numbering():
    assert services.week_day_of(SUNDAY) == WeekDay.SUNDAY == 6
    assert services.week_day_of(SUNDAY + datetime.timedelta(days=1)) == WeekDay.MONDAY == 0


def test_the_branch_options_carry_ids_from_the_branch_table(client_in, station):
    html = client_in.get("/company/branch-working-time/edit/").content.decode()
    assert f'<option value="{station.pk}">Corniche Station</option>' in html


def test_the_branch_in_the_url_is_preselected(client_in, station):
    html = client_in.get(f"/company/branch-working-time/edit/?branch={station.pk}").content.decode()
    assert f'<option value="{station.pk}" selected>' in html


def test_the_page_has_no_kpi_tiles_or_load_button(client_in):
    html = client_in.get("/company/branch-working-time/edit/").content.decode()
    assert "scr-tiles" not in html
    assert "Load Existing Schedule" not in html
    assert "Pick a branch, set its opening" not in html


def test_the_assign_modal_lists_the_branches(client_in, station):
    shift(station, WeekDay.SUNDAY, 1, "08:00", "16:00")
    html = client_in.get("/company/branch-working-time/edit/").content.decode()

    assert "Assign to multiple branches" in html
    assert f'data-wt-assign-row data-id="{station.pk}"' in html
    assert "byky-working-time.js" in html


def test_the_modal_search_cannot_filter_the_week_table(client_in, station):
    """byky-screen.js filters and pages every .scr-row under the nearest card --
    or the whole screen, which is where the modal sits. With those classes the
    modal's search hid the week table's days and injected a "No matching rows"
    row into it. The modal runs its own search, so neither class may appear."""
    html = client_in.get("/company/branch-working-time/edit/").content.decode()
    assert 'class="scr-row' not in html
    assert 'class="scr-search' not in html
    assert 'class="scr-wt-assign-search"' in html


def test_times_use_a_24_hour_picker_not_the_browser_time_input(client_in):
    """<input type="time"> shows AM/PM on a 12-hour computer; flatpickr in
    24-hour mode does not. Both vendor files must be on the page."""
    html = client_in.get("/company/branch-working-time/edit/").content.decode()

    assert 'type="time"' not in html
    assert html.count('class="scr-input scr-shift-time"') == 7 * 4 * 2
    assert "vendor/libs/flatpickr/flatpickr.js" in html
    assert "vendor/libs/flatpickr/flatpickr.css" in html

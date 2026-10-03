"""The dashboard's first row: the branch live carousel, Monthly Sales and the
overdue watch, and the /dashboard/live/ endpoint the page polls."""

import datetime
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.company.models import Branch, BranchType, BranchWorkingTime, Location
from apps.crew.models import Attendance, AttendanceSource, Designation, Employee, PunchType
from apps.devices.models import DeviceMapping, DeviceStatus
from apps.portal import dashboard
from apps.rental.models import Invoice, OrderItem, OrderStatus
from apps.rental.tests.test_order_screens import make_order
from core.ids import uuid7
from core.models import User
from core.timezones import zone_for


@pytest.fixture(autouse=True)
def fresh_cache():
    cache.clear()
    yield
    cache.clear()


def local(world, day, hour, minute=0):
    return datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=zone_for(world["company"]))


def today(world):
    return timezone.now().astimezone(zone_for(world["company"])).date()


_numbers = iter(range(1, 10_000))


def bill(world, shop, when, net, branch=None):
    """A settled order at `branch` and its invoice, issued at `when`."""
    branch = branch or world["adc1"]
    order = make_order(world, shop, f"INV-{next(_numbers)}", status=OrderStatus.COMPLETED, lines=())
    order.branch = branch
    order.save(update_fields=["branch"])
    return Invoice.objects.create(
        id=uuid7(), order=order, company=branch.company, branch=branch, device=shop["tablet"],
        issued_by=User.objects.get(username="sara.k"), invoice_no=order.order_no, issued_at=when,
        company_name="BYKY", company_trn="", branch_name=branch.name, customer_name="Ahmed Al Mansoori",
        customer_mobile="971501234567", subtotal=Decimal(net), discount_amount=0, tax_percentage=5,
        tax_amount=0, rounding_adjustment=0, net_amount=Decimal(net), payments=[],
    )


def hours(branch, week_day, *shifts):
    for number, (start, end) in enumerate(shifts, start=1):
        BranchWorkingTime.objects.create(branch=branch, week_day=week_day, shift_number=number,
                                         start_time=datetime.time(*start), end_time=datetime.time(*end))


def second_station(world, name="Al Ain Zoo"):
    return Branch.objects.create(company=world["company"], location=Location.objects.first(),
                                 short_code=name[:4].upper(), name=name, branch_type=BranchType.STATION)


def punch_in(world, branch, when, code="EMP001"):
    designation = Designation.objects.get_or_create(company=world["company"], code="CASH",
                                                    defaults={"title": "Cashier"})[0]
    employee = Employee.objects.create(company=world["company"], employee_code=code, first_name="Anil",
                                       last_name="K", designation=designation)
    return Attendance.objects.create(
        id=uuid7(), source=AttendanceSource.SELF, punch_type=PunchType.PUNCH_IN, employee=employee,
        employee_code=code, employee_name="Anil K", employee_branch=branch, rms_installation_id="phone-1",
        rms_scan_time=when,
    )


# -- The branch's day ---------------------------------------------------------------------


def test_today_starts_at_the_first_shift_and_shows_the_raw_hours(world):
    day = today(world)
    hours(world["adc1"], day.weekday(), ((7, 0), (12, 0)), ((14, 0), (23, 0)))

    [state] = dashboard.branch_days([world["adc1"].pk], local(world, day, 10), zone_for(world["company"])).values()

    assert state == {"hours": "07:00–12:00 · 14:00–23:00", "state": "open", "starts": local(world, day, 7)}


@pytest.mark.parametrize("hour, expected", [(13, "closed"), (23, "open"), (6, "closed")])
def test_open_means_inside_a_shift_end_minute_included(world, hour, expected):
    day = today(world)
    hours(world["adc1"], day.weekday(), ((7, 0), (12, 0)), ((14, 0), (23, 0)))

    [state] = dashboard.branch_days([world["adc1"].pk], local(world, day, hour), zone_for(world["company"])).values()

    assert state["state"] == expected


def test_a_day_off_and_a_branch_with_no_hours_count_the_calendar_day(world):
    day = today(world)
    hours(world["adc1"], (day.weekday() + 1) % 7, ((7, 0), (23, 0)))      # works tomorrow only
    zoo = second_station(world)

    days = dashboard.branch_days([world["adc1"].pk, zoo.pk], local(world, day, 10), zone_for(world["company"]))

    assert days[world["adc1"].pk] == {"hours": "Closed today", "state": "closed", "starts": local(world, day, 0)}
    assert days[zoo.pk] == {"hours": "No hours set", "state": "not_set", "starts": local(world, day, 0)}


# -- The carousel ---------------------------------------------------------------------------


def test_each_slide_counts_only_its_station_and_today(user, world, shop):
    day = today(world)
    now = local(world, day, 18)
    adc1 = world["adc1"]
    hours(adc1, day.weekday(), ((7, 0), (23, 0)))
    zoo = second_station(world)

    make_order(world, shop, "OUT-1")                                    # one bike out at Corniche 1
    bill(world, shop, local(world, day, 9), "60.00")
    bill(world, shop, local(world, day, 12), "40.50")
    bill(world, shop, local(world, day, 6, 30), "500.00")              # before opening
    bill(world, shop, local(world, day, 9) - datetime.timedelta(days=1), "70.00")    # yesterday
    bill(world, shop, local(world, day, 9), "25.00", branch=zoo)

    tablet = shop["tablet"]
    DeviceMapping.objects.create(device=tablet, branch=adc1, from_date=now - datetime.timedelta(days=3))
    tablet.last_seen_at = local(world, day, 8)
    tablet.save(update_fields=["last_seen_at"])
    stale = type(tablet).objects.create(company=world["company"], installation_id="till-2", platform="android",
                                        channel=tablet.channel, status=DeviceStatus.APPROVED, approved_at=now,
                                        last_seen_at=local(world, day, 6))           # before opening
    DeviceMapping.objects.create(device=stale, branch=adc1, from_date=now - datetime.timedelta(days=3))

    punch_in(world, adc1, local(world, day, 6, 45))                      # early, still counts
    gone = punch_in(world, adc1, local(world, day, 7), code="EMP002")
    Attendance.objects.create(
        id=uuid7(), source=AttendanceSource.SELF, punch_type=PunchType.PUNCH_OUT, punch_in=gone,
        employee=gone.employee, employee_code="EMP002", employee_name="Anil K", employee_branch=adc1,
        rms_installation_id="phone-1", rms_scan_time=local(world, day, 15),
    )

    slides = {s["name"]: s for s in dashboard.branch_live(user, now)}

    corniche = slides["Abu Dhabi Corniche 1"]
    assert (corniche["on_rent"], corniche["invoices"], corniche["revenue"]) == (1, 2, "100.50")
    assert (corniche["devices"], corniche["staff"], corniche["hours"], corniche["state"]) == (
        1, 1, "07:00–23:00", "open")
    assert (slides["Al Ain Zoo"]["invoices"], slides["Al Ain Zoo"]["revenue"]) == (1, "25.00")
    assert "Their Station" not in slides


def test_the_busiest_station_comes_first(user, world, shop):
    zoo = second_station(world, "Zoo Park")
    quiet = second_station(world, "Quiet Beach")
    make_order(world, shop, "OUT-1")
    bill(world, shop, timezone.now(), "10.00", branch=zoo)

    names = [s["name"] for s in dashboard.branch_live(user)]

    assert names == ["Abu Dhabi Corniche 1", "Zoo Park", quiet.name]


# -- Monthly Sales -------------------------------------------------------------------------


def test_the_month_so_far_against_the_same_days_last_month(user, world, shop):
    now = local(world, datetime.date(2026, 10, 15), 12)
    bill(world, shop, local(world, datetime.date(2026, 10, 3), 10), "100.00")
    bill(world, shop, local(world, datetime.date(2026, 10, 10), 10), "50.00")
    bill(world, shop, local(world, datetime.date(2026, 10, 15), 10), "20.00")
    bill(world, shop, local(world, datetime.date(2026, 9, 5), 10), "100.00")
    bill(world, shop, local(world, datetime.date(2026, 9, 15), 13), "999.00")     # later in the day than now
    bill(world, shop, local(world, datetime.date(2026, 9, 20), 10), "999.00")

    sales = dashboard.monthly_sales(user, now)

    assert (sales["title"], sales["total"], sales["daily_avg"]) == ("October 2026 to date", "170", "11")
    assert sales["delta"] == {"text": "+70.0%", "direction": "up"}
    assert len(sales["series"]) == 15 and sales["series"][2] == 100.0 and sales["series"][3] == 0.0
    assert sales["labels"][0] == "1 Oct"
    assert sales["peak"] == "Peak 3 Oct · AED 100"
    assert sales["best_day"] == "Best day: Saturday"


def test_a_month_below_last_month_reads_down(user, world, shop):
    now = local(world, datetime.date(2026, 10, 15), 12)
    bill(world, shop, local(world, datetime.date(2026, 10, 2), 10), "50.00")
    bill(world, shop, local(world, datetime.date(2026, 9, 2), 10), "100.00")

    assert dashboard.monthly_sales(user, now)["delta"] == {"text": "-50.0%", "direction": "down"}


def test_a_month_with_nothing_to_compare_or_show(user, world):
    sales = dashboard.monthly_sales(user, local(world, datetime.date(2026, 10, 3), 12))

    assert (sales["total"], sales["delta"]["direction"], sales["peak"], sales["best_day"]) == ("0", "none", "", "")
    assert sales["series"] == [0.0, 0.0, 0.0]


def test_the_31st_compares_with_the_whole_of_a_shorter_month(user, world, shop):
    bill(world, shop, local(world, datetime.date(2026, 9, 30), 22), "80.00")
    bill(world, shop, local(world, datetime.date(2026, 10, 31), 9), "40.00")

    sales = dashboard.monthly_sales(user, local(world, datetime.date(2026, 10, 31), 10))

    assert sales["delta"] == {"text": "-50.0%", "direction": "down"}


# -- The overdue watch ---------------------------------------------------------------------


def test_only_vehicles_past_their_booked_end_longest_first(user, world, shop):
    now = timezone.now()
    started = now - datetime.timedelta(hours=2)

    def due(order, vehicle, minutes):
        OrderItem.objects.filter(order=order, vehicle=vehicle).update(
            start_time=started, expected_end_time=now + datetime.timedelta(minutes=minutes))

    late = make_order(world, shop, "LATE", lines=("active", "active"))
    due(late, shop["mo41"], -12)
    due(late, shop["dc02"], -40)
    back = make_order(world, shop, "BACK", lines=("returned",))      # past its end, but returned
    due(back, shop["mo41"], -60)

    watch = dashboard.overdue_watch(user, now)

    assert (watch["on_rent"], watch["overdue"]) == (2, 2)
    assert [r["vehicle"] for r in watch["rows"]] == ["DC 02", "MO 41"]
    row = watch["rows"][0]
    assert (row["station"], row["customer"], row["mobile"], row["url"]) == (
        "Abu Dhabi Corniche 1", "Ahmed Al Mansoori", "+971 50 123 4567", f"/rental/order/{late.pk}/")


@pytest.mark.parametrize("stored, shown", [
    ("971501234567", "+971 50 123 4567"), ("+971501234567", "+971 50 123 4567"),
    ("0501234567", "0501234567"), ("", ""),
])
def test_mobiles_are_shown_in_the_uae_format_when_they_are_uae(stored, shown):
    assert dashboard.format_mobile(stored) == shown


# -- The page and its live endpoint -----------------------------------------------------------


def test_the_dashboard_renders_the_first_row(client_in, world, shop):
    make_order(world, shop, "OUT-1")

    response = client_in.get("/dashboard/")

    assert response.status_code == 200
    html = response.content.decode()
    for text in ("Stations Today", "Abu Dhabi Corniche 1", "Monthly Sales", "Vehicles Now",
                 'data-live-url="/dashboard/live/"', 'id="bd-live"', "View all on rent"):
        assert text in html, text
    assert "Their Station" not in html


def test_the_dashboard_renders_with_no_data_at_all(client_in):
    assert client_in.get("/dashboard/").status_code == 200


def test_the_live_endpoint_answers_json(client_in, world, shop):
    make_order(world, shop, "OUT-1")

    data = client_in.get("/dashboard/live/").json()

    assert set(data) == {"generated_at", "branches", "overdue"}
    assert [b["name"] for b in data["branches"]] == ["Abu Dhabi Corniche 1"]
    assert data["overdue"]["on_rent"] == 1


def test_the_live_endpoint_tells_a_signed_out_page_to_stop(client):
    response = client.get("/dashboard/live/")

    assert response.status_code == 401
    assert response.json() == {"detail": "Signed out."}

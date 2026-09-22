"""Crew: the screens, the four-table save, the posting history, and blocking.

The split into four tables is invisible to whoever is entering data, so the
tests check both halves of that claim: one Save writes them all, and a failure
in any one of them saves none.
"""

import datetime
import json

import pytest
from django.utils import timezone

from apps.company.models import Branch, BranchType, Company, Country, Location, State
from apps.crew import services
from apps.crew.models import (
    AddressType,
    AttendanceSource,
    BlockAction,
    Designation,
    Employee,
    EmployeeAddress,
    EmployeeBlockLog,
    EmployeeDesignation,
    EmployeeDetail,
)
from apps.portal.models import Role, RolePermission
from apps.portal.services import grant_all
from core.enums import Channel
from core.models import User

PASSWORD = "Byky#2026"

SCREENS = [
    ("/crew/employee/list/", "crew.employee"),
    ("/crew/designation/list/", "crew.designation"),
    ("/crew/address/list/", "crew.employee_address"),
    ("/crew/block-unblock/", "crew.block_unblock"),
    ("/crew/attendance/list/", "crew.attendance"),
]


@pytest.fixture
def world(db):
    country = Country.objects.create(short_code="AE", name="United Arab Emirates")
    state = State.objects.create(country=country, short_code="AUH", name="Abu Dhabi")
    company = Company.objects.create(
        short_code="BYKY", name="BYKY", country=country, state=state,
        phone_number="+9710000000", email="ops@byky.test",
    )
    other = Company.objects.create(
        short_code="OTHER", name="Other Co", country=country, state=state,
        phone_number="+9711111111", email="ops@other.test",
    )
    location = Location.objects.create(
        country=country, state=state, short_code="CORN", name="Corniche"
    )
    branch = Branch.objects.create(
        company=company, location=location, short_code="AUH01",
        name="Corniche Station", branch_type=BranchType.STATION,
    )
    cashier = Designation.objects.create(company=company, code="CASH", title="Cashier")
    supervisor = Designation.objects.create(company=company, code="SUP", title="Supervisor")
    return {
        "company": company, "other": other, "branch": branch, "state": state,
        "country": country, "cashier": cashier, "supervisor": supervisor,
    }


@pytest.fixture
def client_in(client, world):
    role = Role.objects.create(company=world["company"], name="Administrator")
    grant_all(role)
    User.objects.create_user(
        "sara.k", PASSWORD, display_name="Sara K", company=world["company"], role=role,
        allowed_channels=[Channel.WEB],
    )
    client.post("/login/", {"username": "sara.k", "password": PASSWORD})
    client.role = role
    return client


def post(client, url, payload):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


def make_employee(world, code="BYKY0001", **extra):
    employee = Employee.objects.create(
        company=world["company"], employee_code=code, first_name="Rashed",
        last_name="K", designation=world["cashier"], branch=world["branch"], **extra,
    )
    EmployeeDesignation.objects.create(
        employee=employee, designation=world["cashier"],
        branch=world["branch"], from_date=timezone.localdate(),
    )
    return employee


# --- the screens --------------------------------------------------------------

@pytest.mark.parametrize("url,page_code", SCREENS)
def test_a_screen_renders_on_an_empty_database(client_in, url, page_code):
    response = client_in.get(url)

    assert response.status_code == 200
    assert b"byky-sidebar" in response.content


@pytest.mark.parametrize("url,page_code", SCREENS)
def test_a_screen_refuses_a_role_without_permission(client, world, url, page_code):
    role = Role.objects.create(company=world["company"], name="Visitor")
    User.objects.create_user(
        "vis.itor", PASSWORD, display_name="Vis", company=world["company"], role=role,
        allowed_channels=[Channel.WEB],
    )
    client.post("/login/", {"username": "vis.itor", "password": PASSWORD})

    assert client.get(url).status_code == 403


def test_the_screens_render_with_data(client_in, world):
    employee = make_employee(world)
    EmployeeDetail.objects.create(employee=employee, passport_number="P123")
    EmployeeAddress.objects.create(
        employee=employee, address_type=AddressType.UAE, line1="Corniche Road", city="Abu Dhabi"
    )

    assert b"Rashed" in client_in.get("/crew/employee/list/").content
    assert b"Cashier" in client_in.get("/crew/designation/list/").content
    assert b"Corniche Road" in client_in.get("/crew/address/list/").content


def test_an_employee_of_another_company_is_never_shown(client_in, world):
    other_designation = Designation.objects.create(
        company=world["other"], code="CASH", title="Cashier"
    )
    Employee.objects.create(
        company=world["other"], employee_code="OTH1", first_name="Hidden",
        last_name="Person", designation=other_designation,
    )

    body = client_in.get("/crew/employee/list/").content

    assert b"Hidden" not in body


# --- one Save, four tables ----------------------------------------------------

def test_saving_an_employee_writes_every_table(client_in, world):
    response = post(client_in, "/crew/employee/save/", {
        "emp_no": "BYKY0042", "first_name": "Rashed", "last_name": "K",
        "designation": world["cashier"].pk, "branch": world["branch"].pk,
        "passport_no": "P8891234", "passport_expiry": "2030-01-31",
        "visa_no": "V5566", "salary_bank": "ADCB",
        "mobile": "+971500000000", "is_active": True,
    })

    assert response.status_code == 200, response.content
    employee = Employee.objects.get(pk=response.json()["pk"])

    assert employee.company == world["company"]       # from the user, not the payload
    assert employee.detail.passport_number == "P8891234"
    assert employee.detail.passport_expiry == datetime.date(2030, 1, 31)
    assert employee.postings.count() == 1             # the first posting opened
    assert employee.postings.first().to_date is None


def test_a_bad_document_date_saves_nothing_at_all(client_in, world):
    response = post(client_in, "/crew/employee/save/", {
        "emp_no": "BYKY0043", "first_name": "Rashed",
        "designation": world["cashier"].pk,
        "passport_expiry": "not-a-date",
    })

    assert response.status_code == 400
    assert Employee.objects.count() == 0              # not half-written
    assert EmployeeDetail.objects.count() == 0


def test_every_error_comes_back_at_once(client_in):
    response = post(client_in, "/crew/employee/save/", {"emp_no": "", "first_name": ""})

    errors = response.json()["errors"]
    fields = {error["field"] for error in errors}

    assert len(errors) >= 3
    assert "Employee Code" in fields
    assert "employee_code" not in fields              # never a column name


def test_editing_keeps_one_documents_row(client_in, world):
    employee = make_employee(world)
    EmployeeDetail.objects.create(employee=employee, passport_number="OLD")

    post(client_in, "/crew/employee/save/", {
        "pk": employee.pk, "emp_no": employee.employee_code,
        "first_name": "Rashed", "designation": world["cashier"].pk,
        "passport_no": "NEW",
    })

    assert EmployeeDetail.objects.count() == 1
    employee.detail.refresh_from_db()
    assert employee.detail.passport_number == "NEW"


# --- the posting history ------------------------------------------------------

def test_changing_the_designation_closes_the_old_posting(world):
    employee = make_employee(world)
    yesterday = timezone.localdate() - datetime.timedelta(days=1)
    employee.postings.update(from_date=yesterday)

    services.set_designation(employee, world["supervisor"])

    postings = employee.postings.order_by("from_date")
    assert postings.count() == 2
    assert postings.first().to_date == timezone.localdate()     # closed
    assert postings.last().to_date is None                      # current
    employee.refresh_from_db()
    assert employee.designation == world["supervisor"]          # and the column agrees


def test_saving_without_changing_the_designation_adds_no_posting(world):
    employee = make_employee(world)

    services.set_designation(employee, world["cashier"])

    assert employee.postings.count() == 1     # not one row per save


def test_an_employee_has_only_one_open_posting(world):
    from django.db import IntegrityError

    employee = make_employee(world)

    with pytest.raises(IntegrityError):
        EmployeeDesignation.objects.create(
            employee=employee, designation=world["supervisor"],
            from_date=timezone.localdate(),
        )


# --- blocking -----------------------------------------------------------------

def test_blocking_writes_the_log_and_sets_the_flag(client_in, world):
    employee = make_employee(world)

    response = post(client_in, "/crew/employee/block/", {
        "employee": employee.pk, "action": "block",
        "reason": "Document Expiry", "remarks": "Visa lapsed",
    })

    assert response.json()["ok"] is True
    employee.refresh_from_db()
    assert employee.is_blocked is True
    log = EmployeeBlockLog.objects.get()
    assert log.action == BlockAction.BLOCK
    assert log.reason == "Document Expiry"


def test_blocking_needs_a_reason(client_in, world):
    employee = make_employee(world)

    response = post(client_in, "/crew/employee/block/", {
        "employee": employee.pk, "action": "block",
    })

    assert response.status_code == 400
    employee.refresh_from_db()
    assert employee.is_blocked is False


def test_blocking_signs_the_person_out_everywhere(client_in, world):
    """design/03-login.md section 4: otherwise a blocked cashier keeps working
    until their next sign-in."""
    from apps.portal.auth import sign_in_web
    from apps.portal.session_models import AppSession, LogoutReason

    employee = make_employee(world)
    role = Role.objects.create(company=world["company"], name="Cashier")
    user = User.objects.create_user(
        "byky0001", PASSWORD, display_name="Rashed K", company=world["company"],
        role=role, employee=employee, allowed_channels=[Channel.WEB],
    )
    sign_in_web("byky0001", PASSWORD)
    assert AppSession.objects.filter(user=user, logged_out_at__isnull=True).count() == 1

    post(client_in, "/crew/employee/block/", {
        "employee": employee.pk, "action": "block", "reason": "Investigation",
    })

    session = AppSession.objects.get(user=user)
    assert session.logged_out_at is not None
    assert session.logout_reason == LogoutReason.EMPLOYEE_BLOCKED


def test_unblocking_lets_them_back(client_in, world):
    employee = make_employee(world, is_blocked=True)

    post(client_in, "/crew/employee/block/", {
        "employee": employee.pk, "action": "unblock", "remarks": "Cleared",
    })

    employee.refresh_from_db()
    assert employee.is_blocked is False
    assert EmployeeBlockLog.objects.get().action == BlockAction.UNBLOCK


def test_a_role_without_update_cannot_block(client_in, world):
    employee = make_employee(world)
    RolePermission.objects.filter(
        role=client_in.role, page__code="crew.block_unblock"
    ).update(can_update=False)

    response = post(client_in, "/crew/employee/block/", {
        "employee": employee.pk, "action": "block", "reason": "Investigation",
    })

    assert response.status_code == 403
    employee.refresh_from_db()
    assert employee.is_blocked is False


# --- document expiry ----------------------------------------------------------

def test_expiry_buckets_come_from_real_dates(world):
    today = timezone.localdate()
    expired = make_employee(world, code="E1")
    EmployeeDetail.objects.create(
        employee=expired, visa_expiry=today - datetime.timedelta(days=5)
    )
    soon = make_employee(world, code="E2")
    EmployeeDetail.objects.create(
        employee=soon, visa_expiry=today + datetime.timedelta(days=10)
    )
    fine = make_employee(world, code="E3")
    EmployeeDetail.objects.create(
        employee=fine, visa_expiry=today + datetime.timedelta(days=300)
    )

    summary = {row["key"]: row for row in services.document_expiry(
        Employee.objects.select_related("detail"), today
    )}

    assert summary["visa"]["expired"] == 1
    assert summary["visa"]["nearing"] == 1      # the 300-day one is neither
    assert summary["visa"]["total"] == 2


def test_an_employee_with_no_documents_counts_as_neither(world):
    make_employee(world)

    summary = {row["key"]: row for row in services.document_expiry(Employee.objects.all())}

    assert summary["visa"]["total"] == 0


# --- attendance ---------------------------------------------------------------

def test_attendance_is_read_only(client_in, world):
    """No Add button, for any role -- punches come from the apps."""
    body = client_in.get("/crew/attendance/list/").content

    assert b'data-scr-open="attendance:add"' not in body
    assert b"No attendance recorded yet" in body


def test_a_punch_carries_the_day_it_happened(world):
    """00:20 in Dubai is the previous day in UTC; business_date must follow the
    company, not the server."""
    employee = make_employee(world)
    moment = datetime.datetime(2026, 10, 2, 20, 20, tzinfo=datetime.UTC)

    punch = services.record_punch(
        employee, AttendanceSource.SELF, moment, world["company"]
    )

    assert punch.business_date == datetime.date(2026, 10, 3)
    assert punch.punched_at == moment


def test_an_employee_can_live_where_the_company_has_no_branch(client_in, world):
    """Someone's home is not scoped to their employer's footprint.

    The company operates in Abu Dhabi only; this person lives in Sharjah. The
    address must still be recordable, so the state list on this screen is plain
    geography rather than "where we have branches".
    """
    sharjah = State.objects.create(
        country=world["country"], short_code="SHJ", name="Sharjah"
    )
    employee = make_employee(world)

    assert b"Sharjah" in client_in.get("/crew/address/list/").content

    response = post(client_in, "/crew/address/save/", {
        "employee": employee.pk, "address_type": AddressType.UAE,
        "line1": "Al Majaz", "city": "Sharjah",
        "state": sharjah.pk, "country": world["country"].pk, "is_active": True,
    })

    assert response.json()["ok"] is True, response.content
    assert EmployeeAddress.objects.get().state == sharjah

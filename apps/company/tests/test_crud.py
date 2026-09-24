"""Writing: who may, what happens, and what a delete means.

The permission tests post directly rather than checking that a button is hidden.
Hiding a control is a courtesy; the refusal is the control, and the legacy's
permissions were decorative precisely because nobody tested this.
"""

import json

import pytest

from apps.company.models import (
    Branch,
    BranchType,
    BranchWorkingTime,
    Company,
    Country,
    Department,
    Location,
    State,
    WeekDay,
)
from apps.portal.models import Role, RolePermission
from apps.portal.services import grant_all
from core.enums import ApprovalStatus, Channel
from core.models import User

PASSWORD = "Byky#2026"


@pytest.fixture
def world(db):
    uae = Country.objects.create(short_code="AE", name="United Arab Emirates")
    abu_dhabi = State.objects.create(country=uae, short_code="AUH", name="Abu Dhabi")
    byky = Company.objects.create(
        short_code="BYKY", name="BYKY", country=uae, state=abu_dhabi,
        phone_number="+9710000000", email="ops@byky.test",
    )
    other = Company.objects.create(
        short_code="OTHER", name="Other Co", country=uae, state=abu_dhabi,
        phone_number="+9711111111", email="ops@other.test",
    )
    corniche = Location.objects.create(
        country=uae, state=abu_dhabi, short_code="CORN", name="Corniche"
    )
    return {"company": byky, "other": other, "location": corniche,
            "country": uae, "state": abu_dhabi}


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


# --- create, update, delete ---------------------------------------------------

def test_create_a_department(client_in, world):
    response = post(client_in, "/company/department/save/", {"short_code": "OPS", "name": "Operations"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["message"] == "Department saved."

    department = Department.objects.get(pk=body["pk"])
    assert department.company == world["company"]        # from the user, not the payload
    assert department.is_active is True
    assert department.approval_status == ApprovalStatus.APPROVED


def test_update_a_department(client_in, world):
    department = Department.objects.create(company=world["company"], short_code="OPS", name="Operations")

    post(client_in, "/company/department/save/",
         {"pk": department.pk, "short_code": "OPS", "name": "Operations & Fleet"})

    department.refresh_from_db()
    assert department.name == "Operations & Fleet"
    assert Department.objects.count() == 1               # updated, not duplicated


def test_delete_an_unused_department(client_in, world):
    department = Department.objects.create(company=world["company"], short_code="OPS", name="Operations")

    response = post(client_in, f"/company/department/{department.pk}/delete/", {})

    assert response.json()["ok"] is True
    assert Department.objects.count() == 0


def test_deleting_a_used_department_deactivates_it_and_says_where(client_in, world):
    department = Department.objects.create(company=world["company"], short_code="OPS", name="Operations")
    branch = Branch.objects.create(
        company=world["company"], location=world["location"], short_code="AUH01",
        name="Corniche Station", branch_type=BranchType.STATION,
    )
    branch.departments.add(department)
    # A department a branch uses cannot simply vanish.
    Branch.objects.filter(pk=branch.pk).update(is_active=True)

    response = post(client_in, f"/company/department/{department.pk}/delete/", {})
    body = response.json()

    # A branch listing this department is a use, even though a many-to-many
    # raises nothing: deleting would silently drop it from that branch.
    assert response.status_code == 409
    assert body["code"] == "in_use"
    assert body["links"] == [{"label": "Branches", "count": 1}]
    department.refresh_from_db()
    assert department.is_active is False


def test_deleting_a_referenced_location_deactivates_it(client_in, world):
    """Branch.location is PROTECT, so the database itself refuses."""
    Branch.objects.create(
        company=world["company"], location=world["location"], short_code="AUH01",
        name="Corniche Station", branch_type=BranchType.STATION,
    )

    response = post(client_in, f"/company/location/{world['location'].pk}/delete/", {})
    body = response.json()

    assert response.status_code == 409
    assert body["code"] == "in_use"
    assert body["links"] == [{"label": "Branches", "count": 1}]
    assert "deactivated" in body["note"]
    world["location"].refresh_from_db()
    assert world["location"].is_active is False


# --- validation ---------------------------------------------------------------

def test_every_error_comes_back_at_once(client_in):
    response = post(client_in, "/company/branch/save/", {"short_code": "", "name": ""})

    assert response.status_code == 400
    errors = response.json()["errors"]
    fields = {error["field"] for error in errors}

    assert len(errors) >= 3                       # not just the first one
    assert "Branch Code" in fields                # labelled as the drawer labels it
    assert "short_code" not in fields             # never a column name


def test_a_duplicate_code_is_reported_in_words(client_in, world):
    Department.objects.create(company=world["company"], short_code="OPS", name="Operations")

    response = post(client_in, "/company/department/save/", {"short_code": "OPS", "name": "Ops Two"})

    assert response.status_code == 400
    assert any("already exists" in error["message"] for error in response.json()["errors"])


def test_a_location_must_sit_in_its_own_country(client_in, world):
    kuwait = Country.objects.create(short_code="KW", name="Kuwait")

    response = post(client_in, "/company/location/save/", {
        "country": kuwait.pk, "state": world["state"].pk, "short_code": "X", "name": "Mismatch",
    })

    assert response.status_code == 400
    assert "is not in Kuwait" in response.json()["errors"][0]["message"]


# --- permission on the write --------------------------------------------------

@pytest.mark.parametrize("action,url,payload", [
    ("create", "/company/department/save/", {"short_code": "X", "name": "X"}),
    ("update", "/company/department/save/", {"pk": 0, "short_code": "X", "name": "X"}),
])
def test_a_role_without_the_action_is_refused(client_in, world, action, url, payload):
    department = Department.objects.create(company=world["company"], short_code="OPS", name="Operations")
    if payload.get("pk") == 0:
        payload["pk"] = department.pk
    RolePermission.objects.filter(
        role=client_in.role, page__code="company.department"
    ).update(**{f"can_{action}": False})

    response = post(client_in, url, payload)

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_a_role_without_delete_is_refused(client_in, world):
    department = Department.objects.create(company=world["company"], short_code="OPS", name="Operations")
    RolePermission.objects.filter(
        role=client_in.role, page__code="company.department"
    ).update(can_delete=False)

    response = post(client_in, f"/company/department/{department.pk}/delete/", {})

    assert response.status_code == 403
    assert Department.objects.count() == 1


# --- scope on the write -------------------------------------------------------

def test_a_company_user_cannot_edit_another_companys_row(client_in, world):
    theirs = Department.objects.create(company=world["other"], short_code="OPS", name="Their Ops")

    response = post(client_in, "/company/department/save/",
                    {"pk": theirs.pk, "short_code": "OPS", "name": "Hijacked"})

    assert response.status_code == 404                   # not even acknowledged
    theirs.refresh_from_db()
    assert theirs.name == "Their Ops"


def test_a_company_user_cannot_post_a_different_company(client_in, world):
    response = post(client_in, "/company/department/save/",
                    {"short_code": "OPS", "name": "Operations", "company": world["other"].pk})

    assert response.json()["ok"] is True
    department = Department.objects.get(pk=response.json()["pk"])
    assert department.company == world["company"]        # the payload was ignored


def test_a_company_user_cannot_delete_another_companys_row(client_in, world):
    theirs = Department.objects.create(company=world["other"], short_code="OPS", name="Their Ops")

    response = post(client_in, f"/company/department/{theirs.pk}/delete/", {})

    assert response.status_code == 404
    assert Department.objects.filter(pk=theirs.pk).exists()


# --- the working-time matrix --------------------------------------------------

def test_the_schedule_saves_as_a_whole(client_in, world):
    branch = Branch.objects.create(
        company=world["company"], location=world["location"], short_code="AUH01",
        name="Corniche Station", branch_type=BranchType.STATION,
    )

    response = post(client_in, "/company/branch-working-time/save/", {
        "branch": branch.pk,
        "shifts": [
            {"week_day": WeekDay.SUNDAY, "shift_number": 1, "start": "08:00", "end": "16:00"},
            {"week_day": WeekDay.MONDAY, "shift_number": 1, "start": "08:00", "end": "16:00"},
            {"week_day": WeekDay.TUESDAY, "shift_number": 1, "start": "", "end": ""},   # not worked
        ],
    })

    assert response.json()["ok"] is True
    assert BranchWorkingTime.objects.filter(branch=branch).count() == 2


def test_saving_the_schedule_replaces_the_old_one(client_in, world):
    branch = Branch.objects.create(
        company=world["company"], location=world["location"], short_code="AUH01",
        name="Corniche Station", branch_type=BranchType.STATION,
    )
    BranchWorkingTime.objects.create(
        branch=branch, week_day=WeekDay.FRIDAY, shift_number=1, start_time="09:00", end_time="17:00"
    )

    post(client_in, "/company/branch-working-time/save/", {
        "branch": branch.pk,
        "shifts": [{"week_day": WeekDay.SUNDAY, "shift_number": 1, "start": "08:00", "end": "16:00"}],
    })

    rows = BranchWorkingTime.objects.filter(branch=branch)
    assert rows.count() == 1
    assert rows.first().week_day == WeekDay.SUNDAY   # the Friday row is gone, not orphaned


def test_half_a_shift_is_refused_by_name(client_in, world):
    branch = Branch.objects.create(
        company=world["company"], location=world["location"], short_code="AUH01",
        name="Corniche Station", branch_type=BranchType.STATION,
    )

    response = post(client_in, "/company/branch-working-time/save/", {
        "branch": branch.pk,
        "shifts": [{"week_day": WeekDay.SUNDAY, "shift_number": 2, "start": "08:00", "end": ""}],
    })

    assert response.status_code == 400
    assert response.json()["errors"][0]["field"] == "Sunday shift 2"


def test_the_schedule_of_another_companys_branch_is_out_of_reach(client_in, world):
    theirs = Branch.objects.create(
        company=world["other"], location=world["location"], short_code="OTH01",
        name="Their Station", branch_type=BranchType.STATION,
    )

    response = post(client_in, "/company/branch-working-time/save/", {
        "branch": theirs.pk,
        "shifts": [{"week_day": WeekDay.SUNDAY, "shift_number": 1, "start": "08:00", "end": "16:00"}],
    })

    assert response.status_code == 400
    assert BranchWorkingTime.objects.count() == 0


# --- branch approval authority -----------------------------------------------
# The three Approval Authority pickers write a second table, so they are not on
# BranchForm. Before this they were collected by the drawer and dropped: three
# filled lists that looked recorded and were not.

@pytest.fixture
def mechanic(world):
    from apps.crew.models import Designation, Employee

    designation = Designation.objects.create(
        company=world["company"], code="MECH", title="Mechanic", rank_order=1
    )
    return Employee.objects.create(
        company=world["company"], employee_code="BYKY001",
        first_name="Anil", last_name="R", designation=designation,
    )


def branch_payload(world, **extra):
    return {
        "code": "C1", "name": "Corniche 1", "location": world["location"].pk,
        "branch_type": BranchType.STATION, "hotel_commission": "0", **extra,
    }


def test_approval_authorities_are_saved(client_in, world, mechanic):
    from apps.company.models import AuthorityType, BranchApprovalAuthority

    response = post(client_in, "/company/branch/save/", branch_payload(
        world, leave_request_authority=[mechanic.pk], maintenance_authority=[mechanic.pk],
    ))
    assert response.status_code == 200, response.content

    rows = BranchApprovalAuthority.objects.filter(branch__short_code="C1")
    assert rows.count() == 2
    assert set(rows.values_list("authority_type", flat=True)) == {
        AuthorityType.LEAVE_REQUEST, AuthorityType.MAINTENANCE,
    }


def test_unpicking_an_approver_removes_their_authority(client_in, world, mechanic):
    from apps.company.models import BranchApprovalAuthority

    post(client_in, "/company/branch/save/", branch_payload(
        world, leave_request_authority=[mechanic.pk]
    ))
    branch = Branch.objects.get(short_code="C1")

    post(client_in, "/company/branch/save/", branch_payload(
        world, pk=branch.pk, leave_request_authority=[]
    ))

    assert BranchApprovalAuthority.objects.filter(branch=branch).count() == 0


def test_another_companys_employee_cannot_be_made_an_approver(client_in, world, mechanic):
    """Posted directly: the drawer only ever offers this company's staff."""
    from apps.company.models import BranchApprovalAuthority
    from apps.crew.models import Designation, Employee

    theirs = Employee.objects.create(
        company=world["other"], employee_code="OTH001", first_name="Not", last_name="Ours",
        designation=Designation.objects.create(
            company=world["other"], code="MECH", title="Mechanic", rank_order=1
        ),
    )

    post(client_in, "/company/branch/save/", branch_payload(
        world, leave_request_authority=[theirs.pk]
    ))

    assert BranchApprovalAuthority.objects.filter(employee=theirs).count() == 0


# -- Branch Code is unique per company, not globally (20 Sep 2026) -----------


def test_two_companies_may_use_the_same_branch_code(world):
    """Multi-company arrived after short_code did; this closes the gap --
    "AUH01" belongs to whichever company registers it, once per company."""
    ours = Branch.objects.create(
        company=world["company"], location=world["location"],
        short_code="AUH01", name="Ours",
    )
    theirs = Branch.objects.create(
        company=world["other"], location=world["location"],
        short_code="AUH01", name="Theirs",
    )
    assert (ours.short_code, theirs.short_code) == ("AUH01", "AUH01")


def test_one_company_cannot_reuse_its_own_branch_code(world):
    from django.db import IntegrityError

    Branch.objects.create(
        company=world["company"], location=world["location"],
        short_code="AUH01", name="First",
    )
    with pytest.raises(IntegrityError):
        Branch.objects.create(
            company=world["company"], location=world["location"],
            short_code="AUH01", name="Second",
        )


def test_the_branch_screen_reports_a_duplicate_code_within_one_company(client_in, world):
    """The form's own validation, not a bare IntegrityError reaching the admin."""
    post(client_in, "/company/branch/save/", branch_payload(world, code="AUH01", name="First"))

    response = post(client_in, "/company/branch/save/", branch_payload(world, code="AUH01", name="Second"))

    assert response.status_code == 400
    assert any("already exists" in e["message"] for e in response.json()["errors"])

"""POST /api/v1/operator/employees (apps/crew/employee_api.py): the employee
master for the tablet -- every field of the company's active, approved
employees (owner, 28 Oct 2026)."""

from apps.crew.models import Designation, Employee
from apps.rental.tests import test_order_api as orders
from apps.rental.tests import test_order_requests as requests_tests
from core.enums import ApprovalStatus

call = orders.call
world, token, fresh_throttle = orders.world, orders.token, orders.fresh_throttle
manager = requests_tests.manager

URL = "/api/v1/operator/employees"


def test_every_field_of_the_active_employees(client, world, token):
    company = world["company"]
    designation = Designation.objects.get(company=company)
    anil = Employee.objects.create(company=company, employee_code="BYKY014", first_name="Anil", last_name="Kumar",
                                   designation=designation, branch=world["adc1"], mobile="971501112233",
                                   nationality="Indian", gender="male", is_blocked=True,
                                   approval_status=ApprovalStatus.APPROVED)
    Employee.objects.create(company=company, employee_code="BYKY015", first_name="Gone", designation=designation,
                            is_active=False, approval_status=ApprovalStatus.APPROVED)
    Employee.objects.create(company=company, employee_code="BYKY016", first_name="New", designation=designation,
                            approval_status=ApprovalStatus.PENDING)
    data = call(client, URL, {}, token=token).json()["data"]
    codes = [e["employee_code"] for e in data["employees"]]
    assert "BYKY014" in codes and "BYKY015" not in codes and "BYKY016" not in codes
    row = next(e for e in data["employees"] if e["employee_id"] == anil.pk)
    assert set(row) == {
        "employee_id", "employee_code", "first_name", "middle_name", "last_name", "full_name", "designation",
        "branch", "photo_url", "attendance_method", "date_of_birth", "gender", "marital_status", "nationality",
        "mobile", "email", "emergency_contact_person", "emergency_phone", "is_blocked"}
    assert (row["full_name"], row["branch"]["code"], row["mobile"], row["is_blocked"]) == (
        "Anil Kumar", world["adc1"].short_code, "971501112233", True)


def test_search_and_the_operator_app_only(client, world, token, manager):
    Employee.objects.filter(employee_code="OPR001").update(approval_status=ApprovalStatus.APPROVED)
    found = call(client, URL, {"search": "opr001"}, token=token).json()["data"]["employees"]
    assert [e["employee_code"] for e in found] == ["OPR001"]
    assert call(client, URL.replace("operator", "manager"), {}, token=manager).json()["code"] == "wrong_channel"

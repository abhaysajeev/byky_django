"""The Customer screen and its write endpoints: who may, what they see, what
comes back."""

from apps.portal.models import RolePermission
from apps.rental.models import Customer
from apps.rental.tests.conftest import make_customer, post

SAVE = "/rental/customer/save/"


def revoke(client, *actions, page="rental.customer"):
    RolePermission.objects.filter(role=client.role, page__code=page).update(**{f"can_{a}": False for a in actions})


def customer_data(world, **changes):
    values = {
        "first_name": "Priya", "last_name": "Nair", "gender": "female",
        "mobile_country_code": "+91", "mobile_no": "9876543210",
        "email": "priya.nair@example.com",
    }
    values.update(changes)
    return values


# -- Screen -------------------------------------------------------------------------


def test_the_list_shows_this_companys_customers_only(client_in, world):
    make_customer(world, customer_code="CU001", mobile_no="501111111")
    make_customer(world, company=world["other"], customer_code="CU002", mobile_no="501111112")
    html = client_in.get("/rental/customer/list/").content.decode()
    assert "CU001" in html
    assert "CU002" not in html


def test_a_blocked_customer_shows_blocked(client_in, world):
    make_customer(world, customer_code="CU001", mobile_no="501111111", is_blocked=True, block_reason="Damage")
    html = client_in.get("/rental/customer/list/").content.decode()
    assert "Blocked" in html


def test_the_list_needs_read(client_in):
    revoke(client_in, "read")
    assert client_in.get("/rental/customer/list/").status_code == 403


# -- Save --------------------------------------------------------------------------


def test_save_creates_a_customer_with_a_generated_code(client_in, world):
    body = post(client_in, SAVE, customer_data(world)).json()
    assert body["ok"] is True and body["message"] == "Customer saved."
    customer = Customer.objects.get(pk=body["pk"])
    assert customer.company == world["company"]
    assert customer.customer_code == "CU001"


def test_save_generates_the_next_code_per_company(client_in, world):
    post(client_in, SAVE, customer_data(world, mobile_no="501111111"))
    body = post(client_in, SAVE, customer_data(world, mobile_no="501111112")).json()
    assert Customer.objects.get(pk=body["pk"]).customer_code == "CU002"


def test_document_no_is_not_mandatory(client_in, world):
    body = post(client_in, SAVE, customer_data(world, id_no="")).json()
    assert body["ok"] is True


def test_save_refuses_a_duplicate_phone_number(client_in, world):
    make_customer(world, customer_code="CU001", mobile_no="501234567")
    response = post(client_in, SAVE, customer_data(world, mobile_no="501234567"))
    assert response.status_code == 400
    # A UniqueConstraint violation lands on __all__ (Django's own behaviour,
    # not per-field), so the check is on the message, not e["field"].
    messages = [e["message"] for e in response.json()["errors"]]
    assert any("phone number already exists" in m for m in messages)


def test_save_lists_every_problem(client_in, world):
    response = post(client_in, SAVE, customer_data(world, first_name="", mobile_no=""))
    assert response.status_code == 400
    fields = [e["field"] for e in response.json()["errors"]]
    assert "First Name" in fields and "Phone No" in fields


def test_save_needs_the_right_permission(client_in, world):
    revoke(client_in, "create")
    assert post(client_in, SAVE, customer_data(world)).status_code == 403
    customer = make_customer(world, customer_code="CU001", mobile_no="501111111")
    revoke(client_in, "update")
    assert post(client_in, SAVE, customer_data(world, pk=customer.pk)).status_code == 403


def test_an_edit_keeps_its_code(client_in, world):
    customer = make_customer(world, customer_code="CU001", mobile_no="501111111")
    body = post(client_in, SAVE, customer_data(
        world, pk=customer.pk, mobile_no="501111111", first_name="Renamed")).json()
    assert body["ok"] is True
    customer.refresh_from_db()
    assert customer.customer_code == "CU001" and customer.first_name == "Renamed"


# -- Delete --------------------------------------------------------------------------


def test_delete_removes_the_customer(client_in, world):
    customer = make_customer(world, customer_code="CU001", mobile_no="501111111")
    body = post(client_in, f"/rental/customer/{customer.pk}/delete/", {}).json()
    assert body["ok"] is True
    assert not Customer.objects.exists()


def test_another_companys_customer_is_not_found(client_in, world):
    theirs = make_customer(world, company=world["other"], customer_code="CU001", mobile_no="501111111")
    assert post(client_in, f"/rental/customer/{theirs.pk}/delete/", {}).status_code == 404

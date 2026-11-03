"""Blocked Customers (CR4, 3 Nov 2026): block / unblock with a reason, from the
dashboard and the Customer form, every change logged."""

from apps.portal.models import RolePermission
from apps.rental.models import Customer, CustomerBlockLog
from apps.rental.tests.conftest import make_customer, post

LIST = "/rental/customer/blocked/"
CHANGE = "/rental/customer/blocked/change/"
EXPORT = "/rental/customer/blocked/export/"
SAVE = "/rental/customer/save/"


def revoke(client, *actions, page="rental.customer"):
    RolePermission.objects.filter(role=client.role, page__code=page).update(**{f"can_{a}": False for a in actions})


def test_block_from_the_dashboard_logs_the_reason(client_in, world):
    c = make_customer(world)
    body = post(client_in, CHANGE, {"customer": c.pk, "blocked": True, "reason": "Damage"}).json()
    assert body["ok"] is True
    c.refresh_from_db()
    assert c.is_blocked and c.block_reason == "Damage"
    entry = CustomerBlockLog.objects.get(customer=c)
    assert entry.action == "block" and entry.reason == "Damage"


def test_unblock_needs_a_reason_and_logs_it(client_in, world):
    c = make_customer(world, is_blocked=True, block_reason="Damage")
    assert post(client_in, CHANGE, {"customer": c.pk, "blocked": False, "reason": ""}).status_code == 400
    assert post(client_in, CHANGE, {"customer": c.pk, "blocked": False, "reason": "Paid"}).json()["ok"]
    c.refresh_from_db()
    assert not c.is_blocked and c.block_reason == ""
    assert CustomerBlockLog.objects.get(customer=c).action == "unblock"


def test_no_change_is_refused(client_in, world):
    c = make_customer(world)
    assert post(client_in, CHANGE, {"customer": c.pk, "blocked": False, "reason": "x"}).status_code == 400


def test_change_needs_customer_update(client_in, world):
    c = make_customer(world)
    revoke(client_in, "update")
    assert post(client_in, CHANGE, {"customer": c.pk, "blocked": True, "reason": "x"}).status_code == 403
    assert not Customer.objects.get(pk=c.pk).is_blocked


def test_another_companys_customer_is_not_found(client_in, world):
    c = make_customer(world, company=world["other"], customer_code="CU009")
    assert post(client_in, CHANGE, {"customer": c.pk, "blocked": True, "reason": "x"}).status_code == 400


def test_the_form_logs_block_and_requires_unblock_reason(client_in, world):
    c = make_customer(world)
    data = {"pk": c.pk, "first_name": "Ahmed", "mobile_country_code": "+971", "mobile_no": "501234567"}
    assert post(client_in, SAVE, {**data, "is_blocked": True, "block_reason": ""}).json()["ok"] is False
    assert post(client_in, SAVE, {**data, "is_blocked": True, "block_reason": "Late"}).json()["ok"]
    assert CustomerBlockLog.objects.filter(customer=c, action="block").count() == 1
    assert post(client_in, SAVE, {**data, "is_blocked": False}).json()["ok"] is False
    assert post(client_in, SAVE, {**data, "is_blocked": False, "unblock_reason": "Sorted"}).json()["ok"]
    assert CustomerBlockLog.objects.filter(customer=c, action="unblock", reason="Sorted").exists()


def test_list_history_and_export(client_in, world):
    c = make_customer(world, first_name="Rashid")
    post(client_in, CHANGE, {"customer": c.pk, "blocked": True, "reason": "Damage"})
    assert "Rashid" in client_in.get(LIST).content.decode()
    history = client_in.get(LIST + "?tab=history&action=block").content.decode()
    assert "Damage" in history
    assert "Rashid" not in client_in.get(LIST + "?tab=history&action=unblock").content.decode()
    response = client_in.get(EXPORT + "?tab=history")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/vnd.openxmlformats")


def test_the_page_needs_read(client_in):
    revoke(client_in, "read", page="rental.blocked_customer")
    assert client_in.get(LIST).status_code == 403

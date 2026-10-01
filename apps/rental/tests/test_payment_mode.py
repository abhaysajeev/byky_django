"""The Payment Mode screen: the master the operator app's /payment-modes reads."""

from apps.company.models import PaymentMode
from apps.portal.models import Page, RolePermission
from apps.rental.tests.conftest import post
from core.enums import ApprovalStatus
from core.models import User

LIST = "/rental/payment-mode/list/"
SAVE = "/rental/payment-mode/save/"


def revoke(client, *actions):
    RolePermission.objects.filter(role=client.role, page__code="rental.payment_mode").update(
        **{f"can_{a}": False for a in actions})


def test_the_screen_sits_under_rental(db):
    page = Page.objects.get(code="rental.payment_mode")
    assert (page.module.code, page.url_name) == ("rental", "rental-payment-mode-list")


def test_the_list_shows_this_companys_modes_last_saved_first(client_in, world):
    cash = PaymentMode.objects.create(company=world["company"], name="Cash")
    PaymentMode.objects.create(company=world["company"], name="Card")
    PaymentMode.objects.create(company=world["other"], name="Their Cheque")

    rows = client_in.get(LIST).context["payment_modes"]
    assert [r["name"] for r in rows] == ["Card", "Cash"]

    cash.save()                                                   # an edit moves it to the top
    response = client_in.get(LIST)
    assert [r["name"] for r in response.context["payment_modes"]] == ["Cash", "Card"]
    body = response.content.decode()
    assert "Their Cheque" not in body and 'data-field="company"' not in body


def test_save_creates_for_the_users_company_with_audit_stamps(client_in, world):
    user = User.objects.get(username="sara.k")
    response = post(client_in, SAVE, {"name": "Cash", "company": world["other"].pk})
    assert response.status_code == 200, response.content
    assert response.json()["message"] == "Payment Mode saved."

    mode = PaymentMode.objects.get(pk=response.json()["pk"])
    assert mode.company == world["company"]                       # never the payload's
    assert (mode.is_active, mode.approval_status) == (True, ApprovalStatus.APPROVED)
    assert (mode.created_by, mode.modified_by) == (user, user)

    response = post(client_in, SAVE, {"pk": mode.pk, "name": "Cash AED", "is_active": False})
    assert response.status_code == 200, response.content
    mode.refresh_from_db()
    assert (mode.name, mode.is_active, mode.created_by) == ("Cash AED", False, user)


def test_a_duplicate_name_is_refused_in_words(client_in, world):
    PaymentMode.objects.create(company=world["company"], name="Cash")
    response = post(client_in, SAVE, {"name": "Cash"})
    assert response.status_code == 400
    assert "already exists" in response.json()["errors"][0]["message"]


def test_the_same_name_in_another_company_saves(client_in, world):
    PaymentMode.objects.create(company=world["other"], name="Cash")
    assert post(client_in, SAVE, {"name": "Cash"}).status_code == 200


def test_another_companys_mode_is_out_of_reach(client_in, world):
    theirs = PaymentMode.objects.create(company=world["other"], name="Cash")
    assert post(client_in, SAVE, {"pk": theirs.pk, "name": "Mine"}).status_code == 404
    assert post(client_in, f"/rental/payment-mode/{theirs.pk}/delete/", {}).status_code == 404


def test_delete_removes_an_unused_mode(client_in, world):
    mode = PaymentMode.objects.create(company=world["company"], name="Cheque")
    assert post(client_in, f"/rental/payment-mode/{mode.pk}/delete/", {}).json()["ok"] is True
    assert not PaymentMode.objects.filter(pk=mode.pk).exists()


def test_each_action_needs_its_permission(client_in, world):
    mode = PaymentMode.objects.create(company=world["company"], name="Cash")
    revoke(client_in, "create", "update", "delete")
    assert post(client_in, SAVE, {"name": "Card"}).status_code == 403
    assert post(client_in, SAVE, {"pk": mode.pk, "name": "Card"}).status_code == 403
    assert post(client_in, f"/rental/payment-mode/{mode.pk}/delete/", {}).status_code == 403
    body = client_in.get(LIST).content.decode()
    assert 'data-scr-open="payment_mode:add"' not in body

    revoke(client_in, "read")
    assert client_in.get(LIST).status_code in (302, 403)

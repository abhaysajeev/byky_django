"""The offer screens and endpoints: who may, what they see, what comes back."""

from apps.fare.models import Offer
from apps.fare.tests.conftest import make_offer, post
from apps.portal.models import RolePermission

SAVE = "/fare/offer/save/"


def revoke(client, *actions, page="fare.offer"):
    RolePermission.objects.filter(role=client.role, page__code=page).update(**{f"can_{a}": False for a in actions})


def offer_data(world, **changes):
    values = {
        "offer_code": "PROMO1", "offer_name": "Promo", "level": "company", "branch": "", "location": "",
        "valid_from": "2026-01-01", "valid_to": "2026-12-31", "is_active": True,
        "promotion_for": "quantity", "inventory_type": "vehicle_type",
        "lower_value": "1", "upper_value": "5", "promotion_type": "amount",
        "time_slab_applicable": False, "free_item_selectable": False, "free_item_selectable_note": "",
        "free_or_offer_price": "free",
        "items": [{"key": "n1", "vehicle_type": world["monaco"].pk, "package_minutes": 30, "value": "10.00"}],
        "free_items": [], "time_slabs": [],
    }
    values.update(changes)
    return values


# -- Screens ------------------------------------------------------------------------


def test_the_list_shows_this_companys_offers_only(client_in, world):
    make_offer(world, code="MINE")
    make_offer(world, code="THEIRS", company=world["other"])
    html = client_in.get("/fare/offer/list/").content.decode()
    assert "MINE" in html
    assert "THEIRS" not in html


def test_the_list_has_a_search_box(client_in, world):
    make_offer(world, code="PROMO1")
    html = client_in.get("/fare/offer/list/").content.decode()
    assert 'data-search-key="offer"' in html


def test_the_add_page_needs_create(client_in):
    revoke(client_in, "create")
    assert client_in.get("/fare/offer/add/").status_code == 403


def test_the_edit_page_carries_the_saved_offer(client_in, world):
    offer = make_offer(world, code="PROMO1")
    html = client_in.get(f"/fare/offer/{offer.pk}/edit/").content.decode()
    assert 'id="offer-initial"' in html and f'"pk": {offer.pk}' in html
    assert '"offer_code": "PROMO1"' in html


def test_without_update_the_edit_page_is_read_only(client_in, world):
    offer = make_offer(world)
    revoke(client_in, "update")
    html = client_in.get(f"/fare/offer/{offer.pk}/edit/").content.decode()
    assert "Read only" in html and "data-offer-save" not in html


def test_another_companys_offer_is_not_found(client_in, world):
    theirs = make_offer(world, company=world["other"])
    assert client_in.get(f"/fare/offer/{theirs.pk}/edit/").status_code == 404
    assert post(client_in, f"/fare/offer/{theirs.pk}/delete/", {}).status_code == 404


# -- Save --------------------------------------------------------------------------


def test_save_creates_an_offer(client_in, world):
    body = post(client_in, SAVE, offer_data(world)).json()
    assert body["ok"] is True and body["message"] == "Offer saved."
    assert Offer.objects.get(pk=body["pk"]).company == world["company"]


def test_save_lists_every_problem(client_in, world):
    response = post(client_in, SAVE, offer_data(world, offer_code="", items=[]))
    assert response.status_code == 400
    fields = [e["field"] for e in response.json()["errors"]]
    assert "Promotion Code" in fields and "Promotion Items" in fields


def test_save_needs_the_right_permission(client_in, world):
    revoke(client_in, "create")
    assert post(client_in, SAVE, offer_data(world)).status_code == 403
    offer = make_offer(world)
    revoke(client_in, "update")
    assert post(client_in, SAVE, offer_data(world, pk=offer.pk)).status_code == 403


# -- Delete --------------------------------------------------------------------------


def test_delete_removes_the_offer_and_its_rows(client_in, world):
    offer = make_offer(world)
    body = post(client_in, f"/fare/offer/{offer.pk}/delete/", {}).json()
    assert body["ok"] is True
    assert not Offer.objects.exists()

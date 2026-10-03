"""The package screens and endpoints: who may, what they see, what comes back."""

import pytest

from apps.fare.models import Package
from apps.fare.tests.conftest import make_package, post
from apps.portal.models import RolePermission

SAVE = "/fare/package/save/"


def revoke(client, *actions, page="fare.package"):
    RolePermission.objects.filter(role=client.role, page__code=page).update(**{f"can_{a}": False for a in actions})


def package_data(world, **changes):
    values = {
        "package_code": "PROMO1", "package_name": "Promo", "level": "company", "branch": "", "location": "",
        "valid_from": "2026-01-01", "valid_to": "2026-12-31", "is_active": True,
        "promotion_for": "quantity", "inventory_type": "vehicle_type",
        "lower_value": "1", "upper_value": "5", "promotion_type": "amount",
        "time_slab_applicable": False, "free_item_selectable": False, "free_item_selectable_note": "",
        "free_or_package_price": "free",
        "items": [{"key": "n1", "vehicle_type": world["monaco"].pk, "package_minutes": 30, "value": "10.00"}],
        "free_items": [], "time_slabs": [],
    }
    values.update(changes)
    return values


# -- Screens ------------------------------------------------------------------------


def test_the_list_shows_this_companys_packages_only(client_in, world):
    make_package(world, code="MINE")
    make_package(world, code="THEIRS", company=world["other"])
    html = client_in.get("/fare/package/list/").content.decode()
    assert "MINE" in html
    assert "THEIRS" not in html


@pytest.mark.parametrize(("old", "new"), [
    ("/fare/offer/list/?q=x", "/fare/package/list/?q=x"),
    ("/fare/offer/add/", "/fare/package/add/"),
    ("/fare/offer/7/edit/", "/fare/package/7/edit/"),
])
def test_the_old_offer_addresses_lead_to_packages(client_in, old, new):
    response = client_in.get(old)
    assert (response.status_code, response["Location"]) == (301, new)


def test_the_screens_say_packages(client_in, world):
    html = client_in.get("/fare/package/list/").content.decode()
    assert "Packages" in html and "Offer" not in html


def test_the_list_has_a_search_box(client_in, world):
    make_package(world, code="PROMO1")
    html = client_in.get("/fare/package/list/").content.decode()
    assert 'data-search-key="package"' in html


def test_the_add_page_needs_create(client_in):
    revoke(client_in, "create")
    assert client_in.get("/fare/package/add/").status_code == 403


def test_the_edit_page_carries_the_saved_package(client_in, world):
    package = make_package(world, code="PROMO1")
    html = client_in.get(f"/fare/package/{package.pk}/edit/").content.decode()
    assert 'id="package-initial"' in html and f'"pk": {package.pk}' in html
    assert '"package_code": "PROMO1"' in html


def test_without_update_the_edit_page_is_read_only(client_in, world):
    package = make_package(world)
    revoke(client_in, "update")
    html = client_in.get(f"/fare/package/{package.pk}/edit/").content.decode()
    assert "Read only" in html and "data-package-save" not in html


def test_another_companys_package_is_not_found(client_in, world):
    theirs = make_package(world, company=world["other"])
    assert client_in.get(f"/fare/package/{theirs.pk}/edit/").status_code == 404
    assert post(client_in, f"/fare/package/{theirs.pk}/delete/", {}).status_code == 404


# -- Save --------------------------------------------------------------------------


def test_save_creates_a_package(client_in, world):
    body = post(client_in, SAVE, package_data(world)).json()
    assert body["ok"] is True and body["message"] == "Package saved."
    assert Package.objects.get(pk=body["pk"]).company == world["company"]


def test_save_lists_every_problem(client_in, world):
    response = post(client_in, SAVE, package_data(world, package_code="", items=[]))
    assert response.status_code == 400
    fields = [e["field"] for e in response.json()["errors"]]
    assert "Package Code" in fields and "Promotion Items" in fields


def test_save_needs_the_right_permission(client_in, world):
    revoke(client_in, "create")
    assert post(client_in, SAVE, package_data(world)).status_code == 403
    package = make_package(world)
    revoke(client_in, "update")
    assert post(client_in, SAVE, package_data(world, pk=package.pk)).status_code == 403


# -- Delete --------------------------------------------------------------------------


def test_delete_removes_the_package_and_its_rows(client_in, world):
    package = make_package(world)
    body = post(client_in, f"/fare/package/{package.pk}/delete/", {}).json()
    assert body["ok"] is True
    assert not Package.objects.exists()

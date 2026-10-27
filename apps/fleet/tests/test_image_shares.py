"""Image Sharing (apps/fleet/image_shares.py): the operator app shares a photo
with remarks; the web views and deletes it; the manager app views it."""

from django.test import Client

from apps.fleet.models import ImageShare
from apps.photos.models import Photo
from apps.photos.tests.test_photos import uploaded
from apps.portal.models import Role, RolePermission
from apps.rental.tests import test_order_api as orders
from apps.rental.tests import test_order_requests as requests_tests
from core.enums import Channel
from core.ids import uuid7
from core.models import User

call = orders.call
world, token, fresh_throttle = orders.world, orders.token, orders.fresh_throttle
manager = requests_tests.manager

SHARE = "/api/v1/operator/image-shares"
LIST = "/api/v1/{}/image-shares/list"


def share_data(client, token, **extra):
    return {"sync_id": str(uuid7()), "photo_id": uploaded(client, token, purpose="image_share"),
            "remarks": "Fence broken near gate 2", "shared_at": "2026-10-26 11:02:00", **extra}


def shared(client, token, **extra):
    body = call(client, SHARE, share_data(client, token, **extra), token=token).json()
    assert body["code"] == "ok", body
    return body["data"]


def web_client(world):
    browser = Client()
    role = Role.objects.get(company=world["company"], name="Operator")
    User.objects.create_user("sara.k", orders.fare_api.PASSWORD, display_name="Sara K", company=world["company"],
                             role=role, allowed_channels=[Channel.WEB])
    browser.post("/login/", {"username": "sara.k", "password": orders.fare_api.PASSWORD})
    browser.role = role
    return browser


def test_share_and_a_resend(client, world, token):
    data = share_data(client, token)
    first = call(client, SHARE, data, token=token).json()
    assert first["code"] == "ok"
    assert (first["data"]["remarks"], first["data"]["station"]) == ("Fence broken near gate 2", world["adc1"].name)
    assert "photo_url" not in first["data"]                         # the tablet does not view photos
    assert call(client, SHARE, data, token=token).json()["code"] == "duplicate"
    assert ImageShare.objects.count() == 1
    assert Photo.objects.get(pk=data["photo_id"]).status == "attached"


def test_photo_and_remarks_are_required_and_the_photo_must_be_a_share_photo(client, world, token):
    data = share_data(client, token)
    assert call(client, SHARE, {**data, "remarks": " "}, token=token).json()["code"] == "invalid_request"
    assert call(client, SHARE, {k: v for k, v in data.items() if k != "photo_id"}, token=token).json()["code"] \
        == "invalid_request"
    card = uploaded(client, token, purpose="card_discount")
    assert call(client, SHARE, {**data, "photo_id": card}, token=token).json()["code"] == "unknown_photo"


def test_lists_on_the_tablet_and_in_the_manager_app(client, world, token, manager):
    shared(client, token)
    mine = call(client, LIST.format("operator"), {"mine": True}, token=token).json()["data"]
    assert mine["total"] == 1
    seen = call(client, LIST.format("manager"), {}, token=manager).json()["data"]
    assert seen["total"] == 1 and seen["shares"][0]["photo_thumb_url"].startswith("/api/v1/manager/photos/")
    assert call(client, LIST.format("manager"), {"state_id": 999}, token=manager).json()["data"]["total"] == 0
    # A manager's token on the operator list, and the other way round, are refused.
    assert call(client, LIST.format("operator"), {}, token=manager).json()["code"] == "wrong_channel"
    assert call(client, LIST.format("manager"), {}, token=token).json()["code"] == "wrong_channel"


def test_web_view_and_delete(client, world, token, manager):
    row = shared(client, token)
    browser = web_client(world)
    page = browser.get("/fleet/image-share/list/")
    assert page.status_code == 200 and len(page.context["rows"]) == 1
    photo = page.context["rows"][0]["full"]
    assert browser.get(photo).status_code == 200

    url = f"/fleet/image-share/{row['share_id']}/delete/"
    RolePermission.objects.filter(role=browser.role, page__code="fleet.image_share").update(can_delete=False)
    assert browser.post(url, data="{}", content_type="application/json").status_code == 403
    RolePermission.objects.filter(role=browser.role, page__code="fleet.image_share").update(can_delete=True)
    assert browser.post(url, data="{}", content_type="application/json").json()["ok"] is True

    # Kept, but out of every list, and its photo is no longer served.
    item = ImageShare.objects.get(pk=row["share_id"])
    assert item.deleted_at is not None and item.deleted_by.username == "sara.k"
    assert browser.get("/fleet/image-share/list/").context["rows"] == []
    assert browser.get(photo).status_code == 404
    assert call(client, LIST.format("manager"), {}, token=manager).json()["data"]["total"] == 0
    assert browser.post(url, data="{}", content_type="application/json").status_code == 400   # already gone


def test_the_web_list_needs_the_read_right(client, world, token):
    browser = web_client(world)
    RolePermission.objects.filter(role=browser.role, page__code="fleet.image_share").update(can_read=False)
    assert browser.get("/fleet/image-share/list/").status_code == 403

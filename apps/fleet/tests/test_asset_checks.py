"""Asset checks (apps/fleet/asset_checks.py): the operator app inspects a
station asset with a photo; it is only recorded. The manager app and the web
view them with photos (owner, 27 Oct 2026)."""

from apps.fleet.models import AssetCheck
from apps.fleet.tests import test_image_shares as shares
from apps.fleet.tests import test_service_requests as reports
from apps.photos.models import Photo
from apps.photos.tests.test_photos import uploaded
from apps.portal.models import RolePermission
from apps.rental.tests import test_order_api as orders
from apps.sync.models import IssueKind
from core.ids import uuid7

call = orders.call
world, token, fresh_throttle = orders.world, orders.token, orders.fresh_throttle
manager, assets = reports.manager, reports.assets

CHECK = "/api/v1/operator/asset-checks"
LIST = "/api/v1/{}/asset-checks/list"
DETAIL = "/api/v1/manager/asset-checks/detail"


def check_data(client, token, asset, **extra):
    return {"sync_id": str(uuid7()), "asset_id": asset.pk, "status": "needs_inspection",
            "photo_id": uploaded(client, token, purpose="asset_check"), "remarks": "Lens cracked",
            "checked_at": "2026-10-27 09:30:00", **extra}


def checked(client, token, asset, **extra):
    body = call(client, CHECK, check_data(client, token, asset, **extra), token=token).json()
    assert body["code"] == "ok", body
    return body["data"]


def test_a_check_and_a_resend(client, world, token, assets):
    data = check_data(client, token, assets["cam"])
    first = call(client, CHECK, data, token=token).json()
    assert first["code"] == "ok"
    row = first["data"]
    assert (row["asset_code"], row["status"], row["remarks"], row["station"]) == (
        "CAM-1", "needs_inspection", "Lens cracked", world["adc1"].name)
    assert row["checked_at"] == "2026-10-27 09:30:00"
    assert "photo_url" not in row                                    # the tablet does not view photos
    assert call(client, CHECK, data, token=token).json()["code"] == "duplicate"
    assert AssetCheck.objects.count() == 1
    assert Photo.objects.get(pk=data["photo_id"]).status == "attached"


def test_remarks_are_optional(client, world, token, assets):
    data = check_data(client, token, assets["cam"], status="active")
    del data["remarks"]
    assert call(client, CHECK, data, token=token).json()["code"] == "ok"
    assert AssetCheck.objects.get().remarks == ""


def test_the_photo_is_required_and_must_be_an_asset_check_photo(client, world, token, assets):
    data = check_data(client, token, assets["cam"])
    assert call(client, CHECK, {k: v for k, v in data.items() if k != "photo_id"}, token=token).json()["code"] \
        == "invalid_request"
    share = uploaded(client, token, purpose="image_share")
    assert call(client, CHECK, {**data, "photo_id": share}, token=token).json()["code"] == "unknown_photo"
    assert call(client, CHECK, {**data, "status": "broken"}, token=token).json()["code"] == "invalid_request"
    assert AssetCheck.objects.count() == 0


def test_only_this_stations_assets(client, world, token, assets):
    for asset, code in ((assets["away"], "asset_not_at_station"), (assets["nowhere"], "asset_not_at_station")):
        assert call(client, CHECK, check_data(client, token, asset), token=token).json()["code"] == code
    data = {**check_data(client, token, assets["cam"]), "asset_id": 999999}
    assert call(client, CHECK, data, token=token).json()["code"] == "unknown_asset"


def test_nothing_else_happens(client, world, token, assets):
    from apps.fleet.models import ServiceRequest
    from apps.notifications.models import Notification

    checked(client, token, assets["cam"])
    assert ServiceRequest.objects.count() == 0 and Notification.objects.count() == 0


def test_the_kind_is_parked_on_its_own(world):
    assert IssueKind.ASSET_CHECK == "asset_check"


def test_the_station_assets_carry_the_last_check(client, world, token, assets):
    listed = call(client, reports.ASSETS, {}, token=token).json()["data"]["assets"]
    assert (listed[0]["last_check_status"], listed[0]["last_checked_at"]) == (None, None)
    checked(client, token, assets["cam"], status="needs_inspection", checked_at="2026-10-27 08:00:00")
    checked(client, token, assets["cam"], status="active", checked_at="2026-10-27 10:00:00")
    listed = call(client, reports.ASSETS, {}, token=token).json()["data"]["assets"]
    assert (listed[0]["last_check_status"], listed[0]["last_checked_at"]) == ("active", "2026-10-27 10:00:00")


def test_lists_on_the_tablet_and_in_the_manager_app(client, world, token, manager, assets):
    checked(client, token, assets["cam"], status="active", checked_at="2026-10-27 08:00:00")
    row = checked(client, token, assets["cam"])
    mine = call(client, LIST.format("operator"), {"mine": True}, token=token).json()["data"]
    assert mine["total"] == 2 and mine["checks"][0]["asset_check_id"] == row["asset_check_id"]   # newest first
    assert "photo_url" not in mine["checks"][0]
    only = call(client, LIST.format("operator"), {"status": "active"}, token=token).json()["data"]
    assert only["total"] == 1

    seen = call(client, LIST.format("manager"), {}, token=manager).json()["data"]
    assert seen["total"] == 2 and seen["checks"][0]["photo_thumb_url"].startswith("/api/v1/manager/photos/")
    assert call(client, LIST.format("manager"), {"status": "needs_inspection"}, token=manager).json()[
        "data"]["total"] == 1
    assert call(client, LIST.format("manager"), {"asset_id": assets["away"].pk}, token=manager).json()[
        "data"]["total"] == 0
    assert call(client, LIST.format("manager"), {"state_id": 999}, token=manager).json()["data"]["total"] == 0
    photo = client.get(seen["checks"][0]["photo_url"], HTTP_AUTHORIZATION=f"Bearer {manager}")
    assert photo.status_code == 200

    # A manager's token on the operator list, and the other way round, are refused.
    assert call(client, LIST.format("operator"), {}, token=manager).json()["code"] == "wrong_channel"
    assert call(client, LIST.format("manager"), {}, token=token).json()["code"] == "wrong_channel"


def test_the_manager_detail(client, world, token, manager, assets):
    row = checked(client, token, assets["cam"])
    one = call(client, DETAIL, {"asset_check_id": row["asset_check_id"]}, token=manager).json()
    assert one["code"] == "ok" and one["data"]["photo_url"].startswith("/api/v1/manager/photos/")
    assert call(client, DETAIL, {"asset_check_id": str(uuid7())}, token=manager).json()["code"] \
        == "unknown_asset_check"
    assert call(client, "/api/v1/operator/asset-checks/detail", {"asset_check_id": row["asset_check_id"]},
                token=token).json()["code"] == "wrong_channel"


def test_web_list_is_read_only(client, world, token, assets):
    checked(client, token, assets["cam"])
    checked(client, token, assets["cam"], status="active", remarks="")
    browser = shares.web_client(world)
    page = browser.get("/fleet/asset-check/list/")
    assert page.status_code == 200 and len(page.context["rows"]) == 2
    assert len(browser.get("/fleet/asset-check/list/?status=needs_inspection").context["rows"]) == 1
    assert len(browser.get("/fleet/asset-check/list/?q=CAM-1").context["rows"]) == 2
    assert browser.get(page.context["rows"][0]["full"]).status_code == 200

    assets_page = browser.get("/fleet/asset/list/")
    assert assets_page.status_code == 200

    RolePermission.objects.filter(role=browser.role, page__code="fleet.asset_check").update(can_read=False)
    assert browser.get("/fleet/asset-check/list/").status_code == 403

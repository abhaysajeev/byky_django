"""Photos: upload (POST /api/v1/operator/photos), previews, attaching to a
card-discount request, viewing (manager app and web), and the cleanup of
photos no request took."""

import datetime
import io
import os
import uuid

import pytest
from django.core.files.storage import storages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from PIL import Image

from apps.discount.models import CardDiscountClaim
from apps.photos import services
from apps.photos.models import Photo, PhotoStatus
from apps.portal.models import Role
from apps.portal.services import grant_all
from apps.rental.tests import test_order_requests as base
from apps.rental.tests.test_order_api import call
from core.enums import Channel
from core.ids import uuid7
from core.models import User

world, token, fresh_throttle, shop, booked, manager, card = (
    base.world, base.token, base.fresh_throttle, base.shop, base.booked, base.manager, base.card)

UPLOAD = "/api/v1/{}/photos"
APPROVAL = "/api/v1/operator/card-discounts/approval"


def image_bytes(width=1600, height=1200, fmt="JPEG", orientation=None, quality=80):
    image = Image.new("RGB", (width, height), (200, 30, 30))
    out = io.BytesIO()
    kwargs = {"quality": quality} if fmt == "JPEG" else {}
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        kwargs["exif"] = exif
    image.save(out, fmt, **kwargs)
    return out.getvalue()


def noise(width=1200, height=1000):
    """A PNG of random pixels -- it cannot compress, so it is several MB."""
    out = io.BytesIO()
    Image.frombytes("RGB", (width, height), os.urandom(width * height * 3)).save(out, "PNG")
    return out.getvalue()


def upload(client, token, data=None, *, photo_id=None, purpose="card_discount", app="operator", name="card.jpg"):
    photo_id = photo_id or str(uuid7())
    body = {"photo_id": photo_id, "purpose": purpose,
            "file": SimpleUploadedFile(name, data if data is not None else image_bytes(), "image/jpeg")}
    return client.post(UPLOAD.format(app), body, HTTP_AUTHORIZATION=f"Bearer {token}")


def uploaded(client, token, **kwargs):
    response = upload(client, token, **kwargs)
    assert response.json()["code"] == "ok", response.json()
    return response.json()["data"]["photo_id"]


def approval(booked, card, shop, **extra):
    return {"sync_id": str(uuid7()), "card_discount_id": card.pk, "full_number": shop["ahmed"].mobile_full,
            "order_id": booked["sync_id"], "bill_amount": "100.000", "discount_percent": "10.00",
            "discount_amount": "10.000", "net_amount": "90.000", "requested_at": "2026-10-02 16:31:00", **extra}


# -- Upload ------------------------------------------------------------------------------


def test_an_operator_uploads_a_photo_and_previews_are_made(client, token, world, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        response = upload(client, token)

    body = response.json()
    assert body["code"] == "ok", body
    assert (body["data"]["width"], body["data"]["height"], body["data"]["status"]) == (1600, 1200, "uploaded")
    photo = Photo.objects.get()
    assert photo.original.startswith(f"{world['company'].pk}/") and photo.original.endswith(".jpg")
    store = storages["photos"]
    for key, long_side in ((photo.display, 1280), (photo.thumb, 320)):
        assert key.endswith(".webp")
        with store.open(key, "rb") as handle, Image.open(handle) as preview:
            assert (preview.format, max(preview.size)) == ("WEBP", long_side)


def test_a_resent_upload_is_a_duplicate(client, token):
    photo_id = str(uuid7())
    uploaded(client, token, photo_id=photo_id)

    again = upload(client, token, photo_id=photo_id)

    assert again.json()["code"] == "duplicate"
    assert Photo.objects.count() == 1


@pytest.mark.parametrize("data, code", [
    (image_bytes(800, 600), "photo_too_small"),
    (b"%PDF-1.4 not an image", "photo_type_not_allowed"),
    (None, "photo_too_large"),                     # noise() below: a PNG well over 1 MB
])
def test_bad_files_are_refused(client, token, data, code):
    response = upload(client, token, data if data is not None else noise())

    assert (response.status_code, response.json()["code"]) == (400, code)
    assert not Photo.objects.exists()


def test_too_many_pixels_is_refused_before_decoding(client, token, settings):
    settings.PHOTO_MAX_PIXELS = 1_000_000

    assert upload(client, token).json()["code"] == "photo_too_large"


def test_a_photo_taken_sideways_is_previewed_upright(client, token, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        uploaded(client, token, data=image_bytes(1600, 1000, orientation=6))

    with storages["photos"].open(Photo.objects.get().display, "rb") as handle, Image.open(handle) as preview:
        assert preview.height > preview.width


def test_an_unreadable_file_is_marked_and_shown_as_uploaded(client, token, world):
    photo_id = uuid.UUID(uploaded(client, token))
    storages["photos"].delete(Photo.objects.get(pk=photo_id).original)
    storages["photos"].save(Photo.objects.get(pk=photo_id).original, io.BytesIO(b"broken"))

    services.make_previews(photo_id)

    photo = Photo.objects.get(pk=photo_id)
    assert photo.unreadable and services.key_for(photo, "thumb") == photo.original


@pytest.mark.parametrize("app, purpose, code", [
    ("manager", "card_discount", "wrong_channel"), ("operator", "selfie", "invalid_request")])
def test_wrong_app_or_purpose(client, token, manager, app, purpose, code):
    response = upload(client, manager if app == "manager" else token, purpose=purpose, app=app)

    assert response.json()["code"] == code


def test_the_log_keeps_a_summary_not_the_image(client, token):
    from apps.monitoring.models import RequestLog

    uploaded(client, token)

    row = RequestLog.objects.filter(path="/api/v1/operator/photos").get()
    assert '"multipart": true' in row.request_body and "image/jpeg" in row.request_body
    assert len(row.request_body) < 1000


# -- Attach to a card discount -------------------------------------------------------------


def test_a_card_discount_request_takes_the_photo(client, token, booked, card, shop):
    photo_id = uploaded(client, token)

    body = call(client, APPROVAL, approval(booked, card, shop, card_photo_id=photo_id), token=token).json()

    assert body["code"] == "ok", body
    claim = CardDiscountClaim.objects.get()
    assert str(claim.photo_id) == photo_id
    photo = Photo.objects.get()
    assert photo.status == PhotoStatus.ATTACHED and photo.attached_at is not None


def test_a_request_before_its_photo_is_told_to_wait(client, token, booked, card, shop):
    response = call(client, APPROVAL, approval(booked, card, shop, card_photo_id=str(uuid7())), token=token)

    assert (response.status_code, response.json()["code"]) == (409, "photo_not_uploaded")
    assert not CardDiscountClaim.objects.exists()


def test_a_photo_is_used_once(client, token, booked, card, shop):
    photo_id = uploaded(client, token)
    assert call(client, APPROVAL, approval(booked, card, shop, card_photo_id=photo_id), token=token).json()[
        "code"] == "ok"
    CardDiscountClaim.objects.update(status="rejected", decided_at=timezone.now())

    response = call(client, APPROVAL, approval(booked, card, shop, card_photo_id=photo_id), token=token)

    assert (response.status_code, response.json()["code"]) == (409, "photo_in_use")


def test_another_companys_photo_cannot_be_attached(client, token, booked, card, shop, world):
    photo_id = uploaded(client, token)
    Photo.objects.update(company=world["other"])

    response = call(client, APPROVAL, approval(booked, card, shop, card_photo_id=photo_id), token=token)

    assert response.json()["code"] == "unknown_photo"


def test_older_builds_still_send_a_link(client, token, booked, card, shop):
    body = call(client, APPROVAL, approval(booked, card, shop, card_photo="https://x.test/c.jpg"), token=token).json()

    assert body["code"] == "ok"
    assert CardDiscountClaim.objects.get().card_photo == "https://x.test/c.jpg"


# -- Viewing --------------------------------------------------------------------------------


@pytest.fixture
def claimed(client, token, booked, card, shop, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        photo_id = uploaded(client, token)
    assert call(client, APPROVAL, approval(booked, card, shop, card_photo_id=photo_id), token=token).json()[
        "code"] == "ok"
    return photo_id


def test_the_manager_sees_the_photo_and_its_urls(client, manager, claimed):
    rows = call(client, "/api/v1/manager/requests/list", {}, token=manager).json()["data"]["requests"]
    details = rows[0]["details"]

    assert details["card_photo_id"] == claimed
    assert details["card_photo_url"] == f"/api/v1/manager/photos/{claimed}?size=display"
    image = client.get(details["card_photo_thumb_url"], HTTP_AUTHORIZATION=f"Bearer {manager}")
    assert (image.status_code, image["Content-Type"]) == (200, "image/webp")
    assert b"".join(image.streaming_content)[:4] == b"RIFF"


def test_another_companys_manager_gets_not_found(client, manager, claimed, world):
    Photo.objects.update(company=world["other"])

    response = client.get(f"/api/v1/manager/photos/{claimed}", HTTP_AUTHORIZATION=f"Bearer {manager}")

    assert (response.status_code, response.json()["code"]) == (404, "not_found")


def test_an_unattached_photo_is_never_shown(client, token, manager):
    photo_id = uploaded(client, token)

    response = client.get(f"/api/v1/manager/photos/{photo_id}", HTTP_AUTHORIZATION=f"Bearer {manager}")

    assert response.status_code == 404


def test_nginx_sends_the_file_on_the_deployments(client, manager, claimed, settings):
    settings.PHOTO_SERVE = "nginx"

    response = client.get(f"/api/v1/manager/photos/{claimed}?size=original", HTTP_AUTHORIZATION=f"Bearer {manager}")

    assert response["X-Accel-Redirect"] == f"/_protected/{Photo.objects.get().original}"
    assert response.content == b""


@pytest.fixture
def web(client, world):
    role = Role.objects.create(company=world["company"], name="Back office")
    grant_all(role)
    User.objects.create_user("office", "Byky#2026", display_name="Office", company=world["company"], role=role,
                             allowed_channels=[Channel.WEB])
    client.post("/login/", {"username": "office", "password": "Byky#2026"})
    return client


def test_the_web_approval_page_shows_the_photo(web, claimed):
    claim = CardDiscountClaim.objects.get()

    page = web.get(f"/discount/approval/{claim.pk}/")
    image = web.get(f"/photos/{claimed}/display/")

    assert f"/photos/{claimed}/display/" in page.content.decode()
    assert image.status_code == 200 and image["Content-Type"] == "image/webp"


def test_the_web_needs_the_approval_screen(web, claimed, world):
    from apps.portal.models import RolePermission

    RolePermission.objects.filter(role__name="Back office", page__code="discount.approval").update(can_read=False)

    assert web.get(f"/photos/{claimed}/display/").status_code == 404


# -- Cleanup --------------------------------------------------------------------------------


def test_unattached_photos_are_deleted_after_48_hours(client, token, claimed, django_capture_on_commit_callbacks):
    stale = uploaded(client, token)
    fresh = uploaded(client, token)
    Photo.objects.filter(pk__in=[stale, claimed]).update(uploaded_at=timezone.now() - datetime.timedelta(hours=49))
    stale_key = Photo.objects.get(pk=stale).original

    with django_capture_on_commit_callbacks(execute=True):       # the files go once the row is gone
        deleted = services.purge_unattached()

    assert deleted == 1
    assert set(map(str, Photo.objects.values_list("pk", flat=True))) == {claimed, fresh}
    assert not storages["photos"].exists(stale_key)

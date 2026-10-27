"""Photo rules: what an upload must be, how a request attaches it, who may see
it, and the cleanup of the ones nobody attached.

The device compresses before it uploads (JPEG, long side 1280-1600 px,
rotation fixed, EXIF removed). The server checks the file and stores it as
sent; only the two previews (display, thumbnail) are made here, by the worker
(apps/photos/tasks.py).
"""

import datetime
import io
import logging
import warnings

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import FileResponse, HttpResponse
from django.utils import timezone
from PIL import Image, ImageOps, UnidentifiedImageError

from apps.photos.models import Photo, PhotoPurpose, PhotoStatus
from core.enums import Channel

log = logging.getLogger(__name__)

# Pillow format -> (content type, file extension). Anything else is refused.
FORMATS = {"JPEG": ("image/jpeg", "jpg"), "PNG": ("image/png", "png"), "WEBP": ("image/webp", "webp")}

# Which app may upload a photo for which purpose.
UPLOADERS = {
    PhotoPurpose.CARD_DISCOUNT: {Channel.OPERATOR},
    PhotoPurpose.SERVICE_REQUEST: {Channel.OPERATOR},
    PhotoPurpose.IMAGE_SHARE: {Channel.OPERATOR},
}

# The previews: (field, long side px, WebP quality).
PREVIEWS = (("display", 1280, 80), ("thumb", 320, 70))
SIZES = ("thumb", "display", "original")


class PhotoRefused(Exception):
    """An upload or attach the server will not take. `code` is the contract
    with the apps; `message` is what the person reads."""

    def __init__(self, code, message, status=400):
        super().__init__(code)
        self.code, self.message, self.status = code, message, status


def storage():
    return storages["photos"]


# -- Upload ------------------------------------------------------------------------------

def _inspect(data):
    """(format, width, height) of an image's bytes, or a refusal. The header
    is read first, so an oversized image is refused before it is decoded."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(data))
            width, height = image.size
            if width * height > settings.PHOTO_MAX_PIXELS:
                raise PhotoRefused("photo_too_large", "The photo has too many pixels.")
            image.verify()
    except PhotoRefused:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning,
            OSError, SyntaxError, ValueError):
        raise PhotoRefused("photo_type_not_allowed", "Send a JPEG, PNG or WebP photo.") from None
    if image.format not in FORMATS:
        raise PhotoRefused("photo_type_not_allowed", "Send a JPEG, PNG or WebP photo.")
    if max(width, height) < settings.PHOTO_MIN_LONG_SIDE:
        raise PhotoRefused(
            "photo_too_small", f"The photo must be at least {settings.PHOTO_MIN_LONG_SIDE} px on its long side.",
        )
    return image.format, width, height


def upload(session, photo_id, purpose, file, now=None):
    """(photo, created) -- created is False when this photo_id was already
    stored (the app resent it), which the app treats as success."""
    company = session.user.employee.company
    existing = Photo.objects.filter(pk=photo_id).first()
    if existing is not None:
        if existing.company_id != company.pk:
            raise PhotoRefused("sync_id_conflict", "That photo_id is already used.", 409)
        return existing, False
    if session.channel not in UPLOADERS.get(purpose, ()):
        raise PhotoRefused("purpose_not_allowed", "This app cannot upload that kind of photo.", 403)
    if file.size > settings.PHOTO_MAX_BYTES:
        raise PhotoRefused("photo_too_large", f"The photo must be at most {settings.PHOTO_MAX_BYTES // 1024} KB.")
    data = file.read()
    fmt, width, height = _inspect(data)
    content_type, extension = FORMATS[fmt]

    now = now or timezone.now()
    key = storage().save(f"{company.pk}/{now:%Y}/{now:%m}/{photo_id}.{extension}", ContentFile(data))
    try:
        with transaction.atomic():
            photo = Photo.objects.create(
                id=photo_id, company=company, purpose=purpose, original=key, content_type=content_type,
                bytes=len(data), width=width, height=height, uploaded_by=session.user,
                installation_id=session.device.installation_id if session.device_id else "",
                uploaded_at=now, created_by=session.user, modified_by=session.user,
            )
    except IntegrityError:
        # Lost a race to the same photo_id: keep the stored one, drop our file.
        storage().delete(key)
        existing = Photo.objects.filter(pk=photo_id, company=company).first()
        if existing is None:
            raise
        return existing, False
    except Exception:
        storage().delete(key)        # no row will ever point at it
        raise
    transaction.on_commit(lambda: queue_previews(photo.pk))
    return photo, True


def queue_previews(photo_id):
    """Ask the worker for the previews. If the queue cannot be reached the
    upload still stands: the hourly make_missing_previews picks it up, and
    until then the original is shown."""
    from apps.photos.tasks import make_previews

    try:
        make_previews.delay(str(photo_id))
    except Exception:  # noqa: BLE001 -- a broker outage must not fail an upload
        log.warning("could not queue previews for photo %s", photo_id, exc_info=True)


# -- Attach ------------------------------------------------------------------------------

def attachable(photo_id, company, purpose):
    """The locked photo a request may take, or a refusal. Call inside the
    request's own transaction, then mark_attached once the request is saved."""
    photo = Photo.objects.select_for_update().filter(pk=photo_id).first()
    if photo is None:
        raise PhotoRefused("photo_not_uploaded",
                           "That photo has not reached the server yet. Upload it, then send this again.", 409)
    if photo.company_id != company.pk or photo.purpose != purpose:
        raise PhotoRefused("unknown_photo", "No such photo for this request.", 400)
    if photo.status == PhotoStatus.ATTACHED:
        raise PhotoRefused("photo_in_use", "That photo is already on another request.", 409)
    return photo


def mark_attached(photo, now=None):
    photo.status, photo.attached_at = PhotoStatus.ATTACHED, now or timezone.now()
    photo.save(update_fields=["status", "attached_at", "modified_on"])


# -- Previews ----------------------------------------------------------------------------

def make_previews(photo_id):
    """The display and thumbnail copies, as WebP without metadata, rotated
    the way the camera meant. A file that cannot be read is marked
    unreadable and shown as uploaded."""
    photo = Photo.objects.filter(pk=photo_id).first()
    if photo is None or photo.unreadable or (photo.display and photo.thumb):
        return photo
    store = storage()
    try:
        with store.open(photo.original, "rb") as handle:
            image = ImageOps.exif_transpose(Image.open(handle))
            image = image.convert("RGB")
        keys = {}
        stem = photo.original.rsplit(".", 1)[0]
        for field, side, quality in PREVIEWS:
            copy = image.copy()
            copy.thumbnail((side, side))
            out = io.BytesIO()
            copy.save(out, "WEBP", quality=quality, method=4)
            keys[field] = store.save(f"{stem}_{field}.webp", ContentFile(out.getvalue()))
    except (OSError, UnidentifiedImageError, ValueError, SyntaxError):
        log.warning("photo %s could not be read for previews", photo_id, exc_info=True)
        Photo.objects.filter(pk=photo.pk).update(unreadable=True, modified_on=timezone.now())
        return photo
    Photo.objects.filter(pk=photo.pk).update(modified_on=timezone.now(), **keys)
    photo.refresh_from_db()
    return photo


def missing_previews():
    """Photos still waiting for a preview (one the upload could not queue)."""
    return Photo.objects.filter(Q(display="") | Q(thumb=""), unreadable=False)


# -- Viewing ----------------------------------------------------------------------------

# Web: which screen's read right lets a user see each purpose's photos.
WEB_PAGES = {
    PhotoPurpose.CARD_DISCOUNT: "discount.approval",
    PhotoPurpose.SERVICE_REQUEST: "fleet.service_request",
    PhotoPurpose.IMAGE_SHARE: "fleet.image_share",
}


def _withdrawn(photo):
    """A photo whose owner was deleted is no longer shown anywhere: today only
    an image share deleted on the web (it is kept, its photo is not served)."""
    if photo.purpose != PhotoPurpose.IMAGE_SHARE:
        return False
    from apps.fleet.models import ImageShare

    return ImageShare.objects.filter(photo=photo, deleted_at__isnull=False).exists()


def can_view_web(user, photo):
    from apps.company.scoping import companies_for
    from apps.portal.services import has_permission

    page = WEB_PAGES.get(photo.purpose)
    return (photo.status == PhotoStatus.ATTACHED and page is not None
            and has_permission(user, page, "read")
            and companies_for(user).filter(pk=photo.company_id).exists()
            and not _withdrawn(photo))


def can_view_app(user, photo, channel):
    """The manager app sees its company's attached photos."""
    employee = getattr(user, "employee", None)
    return (channel == Channel.MANAGER and employee is not None and photo.status == PhotoStatus.ATTACHED
            and photo.company_id == employee.company_id and not _withdrawn(photo))


def key_for(photo, size):
    """The stored file for a size; the original until the preview exists."""
    if size == "thumb" and photo.thumb:
        return photo.thumb
    if size in ("thumb", "display") and photo.display:
        return photo.display
    return photo.original


def serve(photo, size):
    """The photo's file as the reply. On the deployments nginx sends it
    (X-Accel-Redirect to an internal location); locally Django does."""
    key = key_for(photo, size)
    content_type = "image/webp" if key.endswith(".webp") else photo.content_type
    if settings.PHOTO_SERVE == "nginx":
        response = HttpResponse(content_type=content_type)
        response["X-Accel-Redirect"] = f"/_protected/{key}"
    else:
        response = FileResponse(storage().open(key, "rb"), content_type=content_type)
    # The file behind a key never changes: keep it in the viewer's own cache only.
    response["Cache-Control"] = "private, max-age=86400"
    return response


def view_path(app, photo_id, size):
    """The manager app's URL for a photo, relative to the server's base."""
    return f"/api/v1/{app}/photos/{photo_id}?size={size}"


# -- Cleanup -----------------------------------------------------------------------------

def purge_unattached(now=None):
    """Delete photos no request took within PHOTO_UNATTACHED_HOURS: files
    first, then the row. Attached photos are never deleted. Returns the
    count."""
    now = now or timezone.now()
    cutoff = now - datetime.timedelta(hours=settings.PHOTO_UNATTACHED_HOURS)
    stale = Photo.objects.filter(status=PhotoStatus.UPLOADED, uploaded_at__lt=cutoff)
    deleted = 0
    for pk in stale.values_list("pk", flat=True):
        with transaction.atomic():
            # Locked and re-checked, the way attachable() locks it: a request
            # attaching it right now either wins and it stays, or waits and is refused.
            photo = stale.select_for_update().filter(pk=pk).first()
            if photo is None:
                continue
            keys = [key for key in (photo.original, photo.display, photo.thumb) if key]
            photo.delete()
            # The files go only once the row is gone for good.
            transaction.on_commit(lambda keys=keys: [storage().delete(key) for key in keys])
        deleted += 1
    if deleted:
        log.info("deleted %s unattached photos", deleted)
    return deleted

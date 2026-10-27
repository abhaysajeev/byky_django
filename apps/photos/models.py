"""Photos the apps upload -- one shared store every module attaches to.

Upload first (POST /api/v1/{app}/photos), then the module's own request names
the photo by its id: a card-discount approval sends `card_photo_id`. Until a
request takes it a photo is `uploaded`, and one no request took is deleted
after PHOTO_UNATTACHED_HOURS (apps/photos/services.py::purge_unattached).
Attached photos are kept.

The file is private (STORAGES["photos"]): the row keeps only its storage
keys, never a path or a URL, so moving the files to object storage later is a
copy and a settings change.
"""

from django.db import models

from core.models import TimeStampedModel


class PhotoPurpose(models.TextChoices):
    """What a photo is for -- decides who may upload it, what may attach it
    and who may see it (apps/photos/services.py)."""

    CARD_DISCOUNT = "card_discount", "Card discount"
    SERVICE_REQUEST = "service_request", "Service request"
    IMAGE_SHARE = "image_share", "Image share"
    ASSET_CHECK = "asset_check", "Asset check"


class PhotoStatus(models.TextChoices):
    UPLOADED = "uploaded", "Uploaded"
    ATTACHED = "attached", "Attached"


class Photo(TimeStampedModel):
    # The device's own id for the photo (a UUIDv7), so a resent upload is
    # stored once -- it is the upload's sync_id.
    id = models.UUIDField(primary_key=True, editable=False)
    company = models.ForeignKey("company.Company", on_delete=models.PROTECT, related_name="photos")
    purpose = models.CharField(max_length=20, choices=PhotoPurpose.choices)
    status = models.CharField(max_length=10, choices=PhotoStatus.choices, default=PhotoStatus.UPLOADED)

    # Storage keys (STORAGES["photos"]): the file as uploaded, and the two
    # previews the worker makes (blank until made).
    original = models.CharField(max_length=255)
    display = models.CharField(max_length=255, blank=True)
    thumb = models.CharField(max_length=255, blank=True)
    content_type = models.CharField(max_length=20)
    bytes = models.PositiveIntegerField()
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    # Set when the previews could not be made from the file: it is shown as
    # uploaded, never retried.
    unreadable = models.BooleanField(default=False)

    uploaded_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    installation_id = models.CharField(max_length=64, blank=True)
    uploaded_at = models.DateTimeField()
    attached_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "photo"
        ordering = ["-uploaded_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status=PhotoStatus.ATTACHED, attached_at__isnull=False)
                | models.Q(status=PhotoStatus.UPLOADED, attached_at__isnull=True),
                name="photo_attached_has_time",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "uploaded_at"]),
            models.Index(fields=["company", "purpose"]),
        ]

    def __str__(self):
        return f"{self.get_purpose_display()} photo {self.pk}"

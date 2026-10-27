"""The photo APIs -- views only translate HTTP; the rules are in
apps/photos/services.py.

POST /api/v1/{app}/photos               upload one photo (multipart)
GET  /api/v1/{app}/photos/<photo_id>    the photo itself (manager app)
"""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.photos import services
from apps.photos.models import Photo, PhotoPurpose
from apps.portal.authentication import AppJWTAuthentication
from core.api import REQUIRED, envelope, other_app, station_of, uuid7_field
from core.enums import Channel
from core.schema import SERVER_ERROR, envelope_responses


class PhotoUploadRequest(serializers.Serializer):
    photo_id = uuid7_field()
    purpose = serializers.ChoiceField(
        choices=PhotoPurpose.choices,
        error_messages={**REQUIRED, "invalid_choice": "must be card_discount, service_request or image_share"},
    )
    file = serializers.FileField(error_messages={**REQUIRED, "invalid": "must be a file",
                                                 "empty": "must not be empty"})


_UPLOAD_SAMPLE = {
    "photo_id": "01a14a3c-77d2-7f1e-9a10-3f7c2b9d4e51", "purpose": "card_discount", "status": "uploaded",
    "width": 1600, "height": 1200, "bytes": 312874,
}

_UPLOAD_DESCRIPTION = """
Upload one photo, then send its `photo_id` with the request it belongs to --
for a card discount, `card_photo_id` in `card-discounts/approval`; for a
service request or an image share, `photo_id` in `service-requests` /
`image-shares`. A photo serves only the purpose it was uploaded for.

**multipart/form-data**, with the access token in the `Authorization:
Bearer` header as on every call. Fields:

* `photo_id` -- a **UUIDv7** made on the device for this photo and resent
  **unchanged** on retry: a second upload with the same id is answered
  `duplicate` and stores nothing. Make it before uploading, so the request
  can be queued offline with it.
* `purpose` -- `card_discount`, `service_request` or `image_share` (operator app).
* `file` -- the image.

**Compress on the device before uploading:** JPEG, long side **1280-1600
px**, at most **1 MB**, rotated upright, EXIF (GPS) removed. A card must stay
readable: not below 1000 px on the long side. The server checks the file and
stores it as sent; it never re-compresses it.

**Order** -- upload first, then the request. A request whose photo has not
arrived yet is refused `photo_not_uploaded`: keep it queued and send it again
after the upload. A photo no request takes within 48 hours is deleted.

| Refusal | When |
|---|---|
| `photo_too_large` | over 1 MB, or too many pixels |
| `photo_too_small` | long side under 1000 px |
| `photo_type_not_allowed` | not a JPEG, PNG or WebP image (checked by content, not by name) |
| `purpose_not_allowed` | this app cannot upload that purpose |
| `sync_id_conflict` | the photo_id is already used by another company |
"""


class PhotoUploadView(APIView):
    """POST /api/v1/{app}/photos"""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser]

    @extend_schema(
        tags=["Operator Photos"],
        summary="Upload a photo",
        description=_UPLOAD_DESCRIPTION,
        request={"multipart/form-data": PhotoUploadRequest},
        responses=envelope_responses(
            (200, "ok", "Photo uploaded.", _UPLOAD_SAMPLE),
            (200, "duplicate", "Already uploaded.", _UPLOAD_SAMPLE),
            (400, "invalid_request", "photo_id must be a UUIDv7.", {"errors": {"photo_id": "must be a UUIDv7"}}),
            (400, "photo_too_large", "The photo must be at most 1024 KB.", {}),
            (400, "photo_too_small", "The photo must be at least 1000 px on its long side.", {}),
            (400, "photo_type_not_allowed", "Send a JPEG, PNG or WebP photo.", {}),
            (401, "not_authenticated", "Sign in first.", {}),
            (403, "purpose_not_allowed", "This app cannot upload that kind of photo.", {}),
            (403, "wrong_channel", "Not allowed on this app.", {}),
            (409, "sync_id_conflict", "That photo_id is already used.", {}),
            (409, "device_not_mapped", "This device has no station.", {}),
            (409, "branch_inactive", "This station is closed.", {}),
            SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        if app != Channel.OPERATOR:
            return envelope("wrong_channel", "Not allowed on this app.", http_status=403)
        refused = other_app(request, app)
        if refused:
            return refused
        _, refused = station_of(request.auth)
        if refused:
            return refused
        form = PhotoUploadRequest(data=request.data)
        form.is_valid(raise_exception=True)
        values = form.validated_data
        try:
            photo, created = services.upload(request.auth, values["photo_id"], values["purpose"], values["file"])
        except services.PhotoRefused as refusal:
            return envelope(refusal.code, refusal.message, http_status=refusal.status)
        data = {"photo_id": str(photo.pk), "purpose": photo.purpose, "status": photo.status,
                "width": photo.width, "height": photo.height, "bytes": photo.bytes}
        if not created:
            return envelope("duplicate", "Already uploaded.", data)
        return envelope("ok", "Photo uploaded.", data)


class PhotoView(APIView):
    """GET /api/v1/{app}/photos/<photo_id>?size=thumb|display|original"""

    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["Manager Photos"],
        summary="A photo, for viewing",
        description=(
            "The image itself, for a photo on a request the manager can see (e.g. a card discount's "
            "`card_photo_url` / `card_photo_thumb_url`). Send the access token in the `Authorization: Bearer` "
            "header -- an image loader must add it. `size`: `thumb` (320 px, for lists), `display` "
            "(1280 px, the default) or `original` (as uploaded, for zooming). WebP for thumb and display; "
            "until they are made the original is sent. Not found -- also for another company's photo -- "
            "is the JSON envelope `not_found`."
        ),
        parameters=[OpenApiParameter("size", str, enum=list(services.SIZES), required=False)],
        responses={
            200: OpenApiResponse(OpenApiTypes.BINARY, description="The image (image/webp, image/jpeg, ...)"),
            **envelope_responses(
                (401, "not_authenticated", "Sign in first.", {}),
                (403, "wrong_channel", "Not allowed on this app.", {}),
                (404, "not_found", "No such photo.", {}),
            ),
        },
    )
    def get(self, request, app, photo_id):
        if app != Channel.MANAGER:
            return envelope("wrong_channel", "Not allowed on this app.", http_status=403)
        refused = other_app(request, app)
        if refused:
            return refused
        size = request.query_params.get("size") or "display"
        photo = Photo.objects.filter(pk=photo_id).first()
        if size not in services.SIZES or photo is None or not services.can_view_app(request.user, photo, app):
            return envelope("not_found", "No such photo.", http_status=404)
        return services.serve(photo, size)

"""POST /api/v1/integrations/erpnext/<slug> -- ERPNext's push (design §4, §17).

Server to server: no session, no app login. Authenticated by a bearer token
(ErpnextToken), optionally an IP allow-list and a request signature. The view
reads the raw body itself so even a body that is not JSON is kept and shown in
the inbox. One lookup and one insert -- nothing else happens in the request.

The reply carries `status` at the top level, because that is the key
ERPNext's sender reads (rms_sync.send); `code` / `message` / `data` are the
usual RMS envelope beside it.
"""

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    inline_serializer,
)
from rest_framework import serializers, status
from rest_framework.response import Response

from apps.integrations import services
from apps.integrations.erpnext import ACTIONS, SLUGS
from apps.integrations.models import ErpnextToken
from core.api import PublicAPIView
from core.network import client_ip


def _reply(http_status, code, message, data=None, reply_status=None):
    body = {"code": code, "message": message, "data": data or {}}
    if reply_status:
        body = {"status": reply_status, **body}
    return Response(body, status=http_status)


def _refused():
    return _reply(status.HTTP_401_UNAUTHORIZED, "not_authenticated", "Unknown or missing token.")


_EXAMPLE = {
    "event_id": "c0a8f3e2-6b1d-4e0a-9d3c-2f61b7a5e901", "doctype": "Branch", "name": "CREEK PARK 1",
    "action": "create", "modified": "2026-11-19 10:42:07.123456", "sent_at": "2026-11-19 10:42:09",
    "company": "BYKY", "data": {"name": "CREEK PARK 1", "branch": "CREEK PARK 1", "branch_code": "CP1"},
}
_RECEIVED = inline_serializer("ErpnextReceived", {
    "status": serializers.ChoiceField(choices=["received", "duplicate"]),
    "code": serializers.CharField(), "message": serializers.CharField(),
    "data": inline_serializer("ErpnextReceivedData", {"inbox_id": serializers.IntegerField()}),
})
_ERROR = inline_serializer("ErpnextError", {
    "code": serializers.CharField(), "message": serializers.CharField(), "data": serializers.DictField(),
})


class ReceiveView(PublicAPIView):
    parser_classes = []          # the body is read raw, never parsed by DRF

    @extend_schema(
        tags=["ERPNext Integration"], operation_id="erpnext_receive",
        summary="Receive one ERPNext change (byky_app/rms_sync.py)",
        description=(
            "One change to one ERPNext record, in byky_app's envelope. Kept in the RMS inbox "
            "for review; nothing is applied to RMS yet (step 1). Every readable body is answered "
            "200: problems are noted in the inbox, never refused. Same `event_id` again -> "
            "`duplicate`. Signed with X-Byky-Timestamp / X-Byky-Signature only when RMS has a "
            "signing secret set."
        ),
        parameters=[
            OpenApiParameter("slug", OpenApiTypes.STR, OpenApiParameter.PATH, enum=sorted(SLUGS)),
            OpenApiParameter("Authorization", OpenApiTypes.STR, OpenApiParameter.HEADER, required=True,
                             description="Bearer <token>"),
            OpenApiParameter("X-Byky-Timestamp", OpenApiTypes.STR, OpenApiParameter.HEADER, required=False),
            OpenApiParameter("X-Byky-Signature", OpenApiTypes.STR, OpenApiParameter.HEADER, required=False),
        ],
        request=inline_serializer("ErpnextEnvelope", {
            "event_id": serializers.CharField(), "doctype": serializers.CharField(),
            "name": serializers.CharField(), "action": serializers.ChoiceField(choices=list(ACTIONS)),
            "modified": serializers.CharField(), "sent_at": serializers.CharField(),
            "company": serializers.CharField(), "data": serializers.DictField(),
        }),
        examples=[OpenApiExample("Branch create", value=_EXAMPLE, request_only=True)],
        responses={
            200: OpenApiResponse(_RECEIVED, description="Kept (received) or already kept (duplicate)."),
            401: OpenApiResponse(_ERROR, description="Missing / unknown token, IP not allowed, or bad signature."),
            413: OpenApiResponse(_ERROR, description="Body larger than 256 KB."),
        },
    )
    def post(self, request, slug):
        ip = client_ip(request)
        if not services.ip_allowed(ip):
            return _refused()
        token = services.token_for(request.META.get("HTTP_AUTHORIZATION"))
        if token is None:
            return _refused()

        limit = settings.ERPNEXT_MAX_BODY
        try:
            declared = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            declared = 0
        if declared > limit:
            return _too_large(limit)
        try:
            raw = request.body
        except RequestDataTooBig:
            return _too_large(limit)
        if len(raw) > limit:
            return _too_large(limit)

        if not services.signature_ok(request.META.get("HTTP_X_BYKY_TIMESTAMP"),
                                     request.META.get("HTTP_X_BYKY_SIGNATURE"), raw):
            return _reply(status.HTTP_401_UNAUTHORIZED, "bad_signature", "Signature missing, wrong or expired.")

        ErpnextToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
        message, created = services.receive(
            slug, raw, token=token, source_ip=ip, user_agent=request.META.get("HTTP_USER_AGENT"))
        if created:
            return _reply(status.HTTP_200_OK, "ok", "Received.", {"inbox_id": message.pk}, "received")
        return _reply(status.HTTP_200_OK, "duplicate", "Already received.", {"inbox_id": message.pk}, "duplicate")


def _too_large(limit):
    return _reply(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "payload_too_large",
                  f"Body is larger than {limit // 1024} KB.")

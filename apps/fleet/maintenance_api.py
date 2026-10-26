"""Tablet and manager-app calls for Service & Maintenance and Image Sharing --
views only translate HTTP; the rules are in apps/fleet/service_requests.py and
apps/fleet/image_shares.py (design/inventory/service-and-image-sharing.md).

Operator app (the station is the session's, never sent):
  POST /api/v1/operator/assets                    the station's assets
  POST /api/v1/operator/service-requests          report a vehicle or asset
  POST /api/v1/operator/service-requests/list     the station's reports
  POST /api/v1/operator/image-shares              share a photo + remarks
  POST /api/v1/operator/image-shares/list         the station's shares
Manager app (company scope; the list of requests to decide is requests/list,
kind `service`, apps/rental/requests.py):
  POST /api/v1/manager/service-requests/approve
  POST /api/v1/manager/service-requests/reject
  POST /api/v1/manager/image-shares/list          view only
"""

from django.utils import timezone
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.company.scoping import companies_for
from apps.fleet import image_shares, service_requests
from apps.fleet.serializers import (
    AssetsRequest,
    ImageShareCreateRequest,
    ImageShareListRequest,
    ManagerImageShareListRequest,
    ServiceRequestCreateRequest,
    ServiceRequestDecisionRequest,
    ServiceRequestListRequest,
)
from apps.portal.authentication import AppJWTAuthentication
from apps.rental.models import DecisionChannel
from apps.sync import parking
from apps.sync.models import IssueKind
from core.api import envelope, manager_only, request_parts, session_station, station_of
from core.enums import Channel
from core.schema import SERVER_ERROR, envelope_request, envelope_responses
from core.timezones import zone_for

_OPERATOR_COMMON = (
    (401, "not_authenticated", "Sign in first.", {}),
    (403, "wrong_channel", "Not allowed on this app.", {}),
    (409, "device_not_mapped", "This device has no station.", {}),
    (409, "branch_inactive", "This station is closed.", {}),
)
_MANAGER_COMMON = (
    (401, "not_authenticated", "Sign in first.", {}),
    (403, "wrong_channel", "Not allowed on this app.", {}),
)
_PHOTO_ROWS = (
    (409, "photo_not_uploaded", "That photo has not reached the server yet. Upload it, then send this again.", {}),
    (400, "unknown_photo", "No such photo for this request.", {}),
    (409, "photo_in_use", "That photo is already on another request.", {}),
)


def _page(page, rows):
    return {"page": page.number, "pages": page.paginator.num_pages, "total": page.paginator.count, **rows}


class _AppView(APIView):
    authentication_classes = [AppJWTAuthentication]
    permission_classes = [IsAuthenticated]

    @staticmethod
    def operator_station(request, app):
        """(branch, refusal) -- operator app only."""
        if app != Channel.OPERATOR or request.auth.channel != Channel.OPERATOR:
            return None, envelope("wrong_channel", "Not allowed on this app.", http_status=403)
        return session_station(request)

    @staticmethod
    def company_ids(request):
        return list(companies_for(request.user).values_list("id", flat=True))


# -- Assets ------------------------------------------------------------------------------

_ASSETS_SAMPLE = {"assets": [
    {"asset_id": 7, "asset_code": "CCTV-ADC1-01", "asset_type": "CCTV", "brand": "Hikvision",
     "serial_no": "HK2231-778", "description": "Entrance camera", "open_request_no": None},
    {"asset_id": 9, "asset_code": "PRN-ADC1-01", "asset_type": "Printer", "brand": None, "serial_no": "",
     "description": "", "open_request_no": "SRV-ADC1-00012"},
]}


class AssetsView(_AppView):
    """POST /api/v1/{app}/assets -- operator app only."""

    @extend_schema(
        tags=["Operator Service & Maintenance"],
        summary="The assets at this station (to report one for service)",
        description="""
Every active, approved asset whose station is this one (Inventory -> Asset,
"Branch"), in asset-code order. `open_request_no` is the service request still
open on it (pending or under maintenance), else `null` -- a second report of it
is refused (`already_reported`). `request_data` may be empty.
""",
        request=envelope_request("AssetsEnvelope", AssetsRequest, request_data_required=False),
        responses=envelope_responses((200, "ok", "Assets.", _ASSETS_SAMPLE), *_OPERATOR_COMMON, SERVER_ERROR),
    )
    def post(self, request, app):
        branch, refused = self.operator_station(request, app)
        if refused:
            return refused
        return envelope("ok", "Assets.", {"assets": service_requests.station_assets(branch)})


# -- Service requests: report ------------------------------------------------------------

_SERVICE_ROW = {
    "request_sync_id": "01a1a2b3-4c5d-7e6f-8a9b-0c1d2e3f4a5b", "request_no": "SRV-ADC1-00012",
    "status": "pending", "target": "vehicle", "vehicle_id": 1042, "vehicle_identifier": "VB1242",
    "vehicle_name": "MO 42", "asset_id": None, "asset_code": None, "asset_type": None,
    "issue": "Front brake not holding", "requested_by": "Rashed", "requested_at": "2026-10-26 10:15:00",
    "decided_by": None, "decided_channel": None, "decided_at": None, "decision_note": "",
    "completed_by": None, "completed_at": None, "completion_note": "", "has_photo": True,
}

_REPORT_DESCRIPTION = """
Report a **vehicle** or a **station asset** for service. `target` is
`vehicle` (with `vehicle_id`) or `asset` (with `asset_id`, from `assets`);
`issue` is free text (required, at most 500 characters); `photo_id` is
optional -- upload it first with purpose `service_request`. `sync_id` is
this call's id (UUIDv7) and becomes the request's id: a resend is answered
`duplicate` with the request as it stands.

**A reported vehicle cannot be rented from this moment** -- `vehicles` shows
it `under_maintenance`, and a booking, add or replace of it is refused
(`vehicle_under_maintenance`) -- until a manager rejects the report, or the
back office puts it back in service. A vehicle out on rent may be reported:
the ride goes on, and it cannot be rented again afterwards.

The target must be at this station; one open report per vehicle / asset
(`already_reported`). Managers are notified. `request_no` (SRV-<station
code>-00001) is counted per station.
"""


class ServiceRequestCreateView(_AppView):
    """POST /api/v1/{app}/service-requests -- operator app only."""

    park_kind = IssueKind.SERVICE_REQUEST

    @extend_schema(
        tags=["Operator Service & Maintenance"],
        summary="Report a vehicle or a station asset for service",
        description=_REPORT_DESCRIPTION,
        request=envelope_request("ServiceRequestCreateEnvelope", ServiceRequestCreateRequest),
        examples=[OpenApiExample("Vehicle", request_only=True, value={"credentials": {}, "request_data": {
            "sync_id": "01a1a2b3-4c5d-7e6f-8a9b-0c1d2e3f4a5b", "target": "vehicle", "vehicle_id": 1042,
            "issue": "Front brake not holding", "photo_id": "01a1a2b0-1111-7222-8333-444455556666",
            "requested_at": "2026-10-26 10:15:00"}}),
            OpenApiExample("Asset", request_only=True, value={"credentials": {}, "request_data": {
                "sync_id": "01a1a2b3-4c5d-7e6f-8a9b-0c1d2e3f4a5c", "target": "asset", "asset_id": 9,
                "issue": "Printer jams on every receipt", "requested_at": "2026-10-26 10:20:00"}})],
        responses=envelope_responses(
            (200, "ok", "Reported.", _SERVICE_ROW),
            (200, "duplicate", "Already reported.", {**_SERVICE_ROW, "status": "approved"}),
            (400, "invalid_request", "vehicle_id is required for a vehicle.",
             {"errors": {"vehicle_id": "is required for a vehicle"}}),
            (400, "vehicle_not_at_station", "That vehicle is not at this station.", {}),
            (400, "asset_not_at_station", "That asset is not at this station.", {}),
            (404, "unknown_vehicle", "No vehicle with that id.", {}),
            (404, "unknown_asset", "No asset with that id.", {}),
            (409, "already_reported", "Already reported as SRV-ADC1-00011.", {}),
            (409, "sync_id_conflict", "That sync_id is already used.", {}),
            *_PHOTO_ROWS, *_OPERATOR_COMMON, *parking.SCHEMA_ROWS, SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        if app != Channel.OPERATOR or request.auth.channel != Channel.OPERATOR:
            return envelope("wrong_channel", "Not allowed on this app.", http_status=403)
        return parking.handle(request, app, self.park_kind, self.perform)

    def perform(self, session, app, request_data, *, replay=False):
        branch, refused = station_of(session, replay=replay)
        if refused:
            return refused
        form = ServiceRequestCreateRequest(data=request_data)
        form.is_valid(raise_exception=True)
        values = dict(form.validated_data)
        zone = zone_for(branch.company)
        values["requested_at"] = timezone.make_aware(values["requested_at"], zone)
        try:
            req, created = service_requests.report(session, values)
        except service_requests.ServiceRefused as refusal:
            return envelope(refusal.code, refusal.message, http_status=refusal.status)
        req = service_requests.with_rows(type(req).objects.filter(pk=req.pk)).get()
        data = service_requests.request_json(req, zone)
        return envelope("ok", "Reported.", data) if created else envelope("duplicate", "Already reported.", data)


class ServiceRequestListView(_AppView):
    """POST /api/v1/{app}/service-requests/list -- operator app only."""

    @extend_schema(
        tags=["Operator Service & Maintenance"],
        summary="This station's service requests, newest first",
        description="""
The station's service requests, newest first, 50 a page (`page` from 1). Optional
filters: `mine` (only this tablet's), `status` (`pending`, `approved` = under
maintenance, `rejected`, `completed` = back in service), `from_date` / `to_date`
(`YYYY-MM-DD`, company time, both days included). Photos are not served to the
tablet; `has_photo` says whether one was sent.
""",
        request=envelope_request("ServiceRequestListEnvelope", ServiceRequestListRequest,
                                 request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Service requests.", {"requests": [_SERVICE_ROW], "page": 1, "pages": 1, "total": 1}),
            *_OPERATOR_COMMON, SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        branch, refused = self.operator_station(request, app)
        if refused:
            return refused
        _, request_data = request_parts(request)
        form = ServiceRequestListRequest(data=request_data)
        form.is_valid(raise_exception=True)
        values = form.validated_data
        zone = zone_for(branch.company)
        rows, page = service_requests.station_list(
            branch, device=request.auth.device if values["mine"] else None, status=values.get("status") or None,
            from_date=values.get("from_date"), to_date=values.get("to_date"), zone=zone, page=values["page"])
        return envelope("ok", "Service requests.",
                        _page(page, {"requests": [service_requests.request_json(r, zone) for r in rows]}))


# -- Service requests: manager decisions -------------------------------------------------

_DECIDE_DESCRIPTION = """
Decide a **pending** service request (find them in `requests/list`, kind
`service`). **Approve**: the fault is confirmed -- the vehicle stays out of
renting until the back office puts it back in service (web). **Reject**: not a
fault -- the request closes and the vehicle can be rented again at once.
`note` optional (at most 500 characters). The reporting tablet is notified.
"""


class _ServiceDecisionView(_AppView):
    act = None
    done = ""

    def post(self, request, app):
        refused = manager_only(request, app)
        if refused:
            return refused
        _, request_data = request_parts(request)
        form = ServiceRequestDecisionRequest(data=request_data)
        form.is_valid(raise_exception=True)
        values = form.validated_data
        try:
            req = type(self).act(request.user, values["request_sync_id"], self.company_ids(request),
                                 DecisionChannel.MANAGER, values.get("note", ""))
        except service_requests.ServiceRefused as refusal:
            return envelope(refusal.code, refusal.message, http_status=refusal.status)
        req = service_requests.with_rows(type(req).objects.filter(pk=req.pk)).get()
        return envelope("ok", self.done, service_requests.manager_json(req, zone_for(req.company)))


_DECISION_ROWS = (
    (400, "invalid_request", "request_sync_id is required.", {"errors": {"request_sync_id": "is required"}}),
    (404, "unknown_request", "No service request with that id.", {}),
    (409, "request_decided", "This request is already under maintenance.", {}),
)


class ServiceRequestApproveView(_ServiceDecisionView):
    """POST /api/v1/{app}/service-requests/approve -- manager app only."""

    act = staticmethod(service_requests.approve)
    done = "Service request approved."

    @extend_schema(tags=["Manager Requests"], summary="Approve a service request",
                   description=_DECIDE_DESCRIPTION,
                   request=envelope_request("ServiceRequestApproveEnvelope", ServiceRequestDecisionRequest),
                   responses=envelope_responses((200, "ok", "Service request approved.", {}), *_DECISION_ROWS,
                                                *_MANAGER_COMMON, SERVER_ERROR))
    def post(self, request, app):
        return super().post(request, app)


class ServiceRequestRejectView(_ServiceDecisionView):
    """POST /api/v1/{app}/service-requests/reject -- manager app only."""

    act = staticmethod(service_requests.reject)
    done = "Service request rejected."

    @extend_schema(tags=["Manager Requests"], summary="Reject a service request",
                   description=_DECIDE_DESCRIPTION,
                   request=envelope_request("ServiceRequestRejectEnvelope", ServiceRequestDecisionRequest),
                   responses=envelope_responses((200, "ok", "Service request rejected.", {}), *_DECISION_ROWS,
                                                *_MANAGER_COMMON, SERVER_ERROR))
    def post(self, request, app):
        return super().post(request, app)


# -- Image sharing -----------------------------------------------------------------------

_SHARE_ROW = {
    "share_id": "01a1a2c4-0000-7000-8000-000000000001", "remarks": "Fence broken near gate 2",
    "branch_id": 12, "station": "Abu Dhabi Corniche 1", "emirate": "Abu Dhabi", "shared_by": "Rashed",
    "shared_at": "2026-10-26 11:02:00", "photo_id": "01a1a2c3-9999-7888-8777-666655554444",
}
_MANAGER_SHARE_ROW = {
    **_SHARE_ROW,
    "photo_url": "/api/v1/manager/photos/01a1a2c3-9999-7888-8777-666655554444?size=display",
    "photo_thumb_url": "/api/v1/manager/photos/01a1a2c3-9999-7888-8777-666655554444?size=thumb",
}


class ImageShareCreateView(_AppView):
    """POST /api/v1/{app}/image-shares -- operator app only."""

    park_kind = IssueKind.IMAGE_SHARE

    @extend_schema(
        tags=["Operator Image Sharing"],
        summary="Share a photo with remarks",
        description="""
Share a photo with the back office: upload it first (`photos`, purpose
`image_share`), then send its `photo_id` with `remarks` (required, at most 500
characters) and `shared_at`. `sync_id` is this call's id (UUIDv7) and becomes the
share's id: a resend is answered `duplicate`. Seen on the web (Inventory ->
Image Sharing) and in the manager app; nothing to approve.
""",
        request=envelope_request("ImageShareCreateEnvelope", ImageShareCreateRequest),
        responses=envelope_responses(
            (200, "ok", "Shared.", _SHARE_ROW), (200, "duplicate", "Already shared.", _SHARE_ROW),
            (400, "invalid_request", "remarks is required.", {"errors": {"remarks": "is required"}}),
            (409, "sync_id_conflict", "That sync_id is already used.", {}),
            *_PHOTO_ROWS, *_OPERATOR_COMMON, *parking.SCHEMA_ROWS, SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        if app != Channel.OPERATOR or request.auth.channel != Channel.OPERATOR:
            return envelope("wrong_channel", "Not allowed on this app.", http_status=403)
        return parking.handle(request, app, self.park_kind, self.perform)

    def perform(self, session, app, request_data, *, replay=False):
        branch, refused = station_of(session, replay=replay)
        if refused:
            return refused
        form = ImageShareCreateRequest(data=request_data)
        form.is_valid(raise_exception=True)
        values = dict(form.validated_data)
        zone = zone_for(branch.company)
        values["shared_at"] = timezone.make_aware(values["shared_at"], zone)
        try:
            item, created = image_shares.share(session, values)
        except image_shares.ShareRefused as refusal:
            return envelope(refusal.code, refusal.message, http_status=refusal.status)
        item = image_shares.visible(type(item).objects.filter(pk=item.pk)).first() or item
        data = image_shares.share_json(item, zone, Channel.OPERATOR)
        return envelope("ok", "Shared.", data) if created else envelope("duplicate", "Already shared.", data)


class ImageShareListView(_AppView):
    """POST /api/v1/{app}/image-shares/list -- operator app (this station) or
    manager app (every station, view only)."""

    @extend_schema(
        tags=["Operator Image Sharing", "Manager Image Sharing"],
        summary="Shared images, newest first",
        description="""
Shared images, newest first, 50 a page (`page` from 1); deleted ones are left
out. **Operator app**: this station's -- `mine` for only this tablet's; no photo
URLs (the tablet does not view them). **Manager app**: every station of the
company -- `state_id`, `branch_id` -- with `photo_url` / `photo_thumb_url`
(server-relative, need the access token). Both: `from_date` / `to_date`
(`YYYY-MM-DD`, company time, both days included).
""",
        request=envelope_request("ImageShareListEnvelope", ManagerImageShareListRequest,
                                 request_data_required=False),
        responses=envelope_responses(
            (200, "ok", "Shared images.", {"shares": [_MANAGER_SHARE_ROW], "page": 1, "pages": 1, "total": 1}),
            *_OPERATOR_COMMON, SERVER_ERROR,
        ),
    )
    def post(self, request, app):
        _, request_data = request_parts(request)
        if app == Channel.MANAGER:
            refused = manager_only(request, app)
            if refused:
                return refused
            form = ManagerImageShareListRequest(data=request_data)
            form.is_valid(raise_exception=True)
            values = form.validated_data
            zone = zone_for(getattr(request.user, "company", None))
            rows, page = image_shares.manager_list(
                self.company_ids(request), state_id=values.get("state_id"), branch_id=values.get("branch_id"),
                from_date=values.get("from_date"), to_date=values.get("to_date"), zone=zone, page=values["page"])
            return envelope("ok", "Shared images.", _page(page, {
                "shares": [image_shares.share_json(r, zone, Channel.MANAGER) for r in rows]}))
        branch, refused = self.operator_station(request, app)
        if refused:
            return refused
        form = ImageShareListRequest(data=request_data)
        form.is_valid(raise_exception=True)
        values = form.validated_data
        zone = zone_for(branch.company)
        rows, page = image_shares.station_list(
            branch, device=request.auth.device if values["mine"] else None, from_date=values.get("from_date"),
            to_date=values.get("to_date"), zone=zone, page=values["page"])
        return envelope("ok", "Shared images.", _page(page, {
            "shares": [image_shares.share_json(r, zone, Channel.OPERATOR) for r in rows]}))

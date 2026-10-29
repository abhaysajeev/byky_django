"""Service & Maintenance: a vehicle or station asset reported by the operator
app, decided by a manager, put back in service on the web.

Owner decisions, 23-26 Oct 2026 (design/inventory/service-and-image-sharing.md):
- the operator app reports a vehicle or an asset of its station: free-text
  issue, optional photo;
- "under maintenance" is simply an open request on a vehicle (pending or
  approved) -- information only: renting it is **not** refused (the client
  does not want that, 26 Oct 2026); the tablet's vehicle list shows it;
- except warehouse service (owner, 29 Oct 2026): a request of an issue type
  that needs warehouse service, once **approved**, keeps its vehicle off rent
  until it is rejected or back in service -- booking, add / replace and test
  rides refuse it, and the vehicle list says can_rent false. The flag is
  copied onto the request when reported;
- each change reaches the station's other tablets (station_sync);
- a manager (manager app or web) approves or rejects;
- back in service on the web only; numbers SRV-<station code>-00001, counted
  per station.
New: legacy had no maintenance tables (the AMS/EMS areas were switched off).
"""

import datetime

from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.fleet.models import (
    SERVICE_OPEN,
    Asset,
    ServiceRequest,
    ServiceRequestCounter,
    ServiceRequestStatus,
    Vehicle,
)
from apps.notifications import services as notifications
from apps.notifications.models import Action
from apps.photos import services as photo_services
from apps.photos.models import PhotoPurpose
from apps.rental import station_sync
from core.enums import ApprovalStatus, Channel
from core.timezones import zone_for

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
PAGE_SIZE = 50
VEHICLE, ASSET = "vehicle", "asset"


class ServiceRefused(Exception):
    """A report or decision that cannot be done. `message` is shown as it is;
    `status` is the HTTP status the API answers with."""

    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def request_no(branch, number):
    return f"SRV-{branch.short_code.upper()}-{number:05d}"


def under_maintenance(vehicle_ids):
    """{vehicle_id: request_no} of the given vehicles that are under
    maintenance -- an open service request holds them."""
    return dict(ServiceRequest.objects.filter(vehicle_id__in=list(vehicle_ids), status__in=SERVICE_OPEN)
                .values_list("vehicle_id", "request_no"))


def in_warehouse_service(vehicle_ids):
    """{vehicle_id: request_no} of the given vehicles off rent for warehouse
    service: an approved request whose issue type needs it (owner, 29 Oct 2026)."""
    return dict(ServiceRequest.objects.filter(vehicle_id__in=list(vehicle_ids), status=ServiceRequestStatus.APPROVED,
                                              needs_warehouse_service=True)
                .values_list("vehicle_id", "request_no"))


def open_for_assets(asset_ids):
    """{asset_id: request_no} of the given assets with an open request."""
    return dict(ServiceRequest.objects.filter(asset_id__in=list(asset_ids), status__in=SERVICE_OPEN)
                .values_list("asset_id", "request_no"))


# -- Report (operator app) ------------------------------------------------------------


def report(session, values):
    """A new service request from the tablet. Returns (request, created):
    created is False when this sync_id was already stored (a resend).

    The target must be at the tablet's own station; one open request per
    vehicle / asset (also a database rule, uniq_open_service_request_per_*)."""
    branch = session.branch
    company = branch.company
    sync_id = values["sync_id"]
    existing = ServiceRequest.objects.filter(pk=sync_id).first()
    if existing is not None:
        if existing.company_id != company.pk:
            raise ServiceRefused("sync_id_conflict", "That sync_id is already used.")
        return existing, False
    with transaction.atomic():
        return _report(session, values, branch, company, sync_id)


def _target(values, branch, company):
    if values["target"] == VEHICLE:
        vehicle = Vehicle.objects.filter(pk=values["vehicle_id"], company=company, is_active=True).first()
        if vehicle is None:
            raise ServiceRefused("unknown_vehicle", "No vehicle with that id.", 404)
        if vehicle.current_branch_id != branch.pk:
            raise ServiceRefused("vehicle_not_at_station", "That vehicle is not at this station.", 400)
        return vehicle, None
    asset = Asset.objects.filter(pk=values["asset_id"], company=company, is_active=True).first()
    if asset is None:
        raise ServiceRefused("unknown_asset", "No asset with that id.", 404)
    if asset.branch_id != branch.pk:
        raise ServiceRefused("asset_not_at_station", "That asset is not at this station.", 400)
    return None, asset


def _open_refusal(vehicle, asset):
    open_request = ServiceRequest.objects.filter(
        status__in=SERVICE_OPEN, **({"vehicle": vehicle} if vehicle else {"asset": asset}),
    ).first()
    if open_request is None:
        return None
    return ServiceRefused("already_reported", f"Already reported as {open_request.request_no}.")


def _issue_type(values, company):
    """The picked issue type -- this company's, active, and for this kind of
    target (`applies_to`); None when none was picked."""
    from apps.fleet.models import IssueType

    if not values.get("issue_type_id"):
        return None
    issue_type = IssueType.objects.filter(pk=values["issue_type_id"], company=company, is_active=True).first()
    if issue_type is None:
        raise ServiceRefused("unknown_issue_type", "No issue type with that id.", 404)
    if not issue_type.fits(values["target"]):
        raise ServiceRefused("issue_type_not_for_target", f"{issue_type.name} is not for a {values['target']}.", 400)
    return issue_type


def _report(session, values, branch, company, sync_id):
    vehicle, asset = _target(values, branch, company)
    issue_type = _issue_type(values, company)
    refused = _open_refusal(vehicle, asset)
    if refused:
        raise refused
    photo = None
    if values.get("photo_id"):
        try:
            photo = photo_services.attachable(values["photo_id"], company, PhotoPurpose.SERVICE_REQUEST)
        except photo_services.PhotoRefused as refusal:
            raise ServiceRefused(refusal.code, refusal.message, refusal.status) from None

    counter, _ = ServiceRequestCounter.objects.select_for_update().get_or_create(branch=branch)
    counter.last_number += 1
    counter.save(update_fields=["last_number"])

    req = ServiceRequest(
        id=sync_id, company=company, branch=branch, number=counter.last_number,
        request_no=request_no(branch, counter.last_number), vehicle=vehicle, asset=asset,
        issue_type=issue_type, issue=(values.get("issue") or "").strip(), photo=photo, device=session.device,
        needs_warehouse_service=bool(vehicle and issue_type and issue_type.needs_warehouse_service),
        requested_by=session.user,
        requested_at=values["requested_at"], status=ServiceRequestStatus.PENDING,
        created_by=session.user, modified_by=session.user,
    )
    try:
        with transaction.atomic():
            req.save(force_insert=True)
    except IntegrityError:
        # Lost a race: the same sync_id, or another open request on the target.
        raced = ServiceRequest.objects.filter(pk=sync_id).first()
        if raced is not None:
            return raced, False
        raise _open_refusal(vehicle, asset) or ServiceRefused(
            "already_reported", "This is already reported.") from None
    if photo is not None:
        photo_services.mark_attached(photo)
    notifications.notify(req, Action.RAISED)
    station_sync.service_request_changed(req, "raised")
    return req, True


# -- Decide (manager app and web) and back in service (web) ---------------------------


def _locked(request_id, company_ids):
    req = (ServiceRequest.objects.select_for_update()
           .filter(pk=request_id, company_id__in=company_ids).first())
    if req is None:
        raise ServiceRefused("unknown_request", "No service request with that id.", 404)
    return req


def _decide(user, request_id, company_ids, channel, note, status):
    with transaction.atomic():
        req = _locked(request_id, company_ids)
        if req.status != ServiceRequestStatus.PENDING:
            raise ServiceRefused("request_decided", f"This request is already {req.get_status_display().lower()}.")
        req.status, req.decided_by, req.decided_channel = status, user, channel
        req.decided_at, req.decision_note, req.modified_by = timezone.now(), (note or "").strip(), user
        req.save()
        approved = status == ServiceRequestStatus.APPROVED
        notifications.notify(req, Action.APPROVED if approved else Action.REJECTED)
        station_sync.service_request_changed(req, "approved" if approved else "rejected")
    return req


def approve(user, request_id, company_ids, channel, note=""):
    """The fault is confirmed: under maintenance until it is put back in
    service on the web."""
    return _decide(user, request_id, company_ids, channel, note, ServiceRequestStatus.APPROVED)


def reject(user, request_id, company_ids, channel, note=""):
    """Not a fault: the request closes."""
    return _decide(user, request_id, company_ids, channel, note, ServiceRequestStatus.REJECTED)


def complete(user, request_id, company_ids, note):
    """Back in service (web only): the repair is done."""
    note = (note or "").strip()
    if not note:
        raise ServiceRefused("invalid_request", "Say what was done.", 400)
    with transaction.atomic():
        req = _locked(request_id, company_ids)
        if req.status != ServiceRequestStatus.APPROVED:
            raise ServiceRefused("request_not_approved", "Only an approved request can go back in service.")
        req.status, req.completed_by, req.completed_at = ServiceRequestStatus.COMPLETED, user, timezone.now()
        req.completion_note, req.modified_by = note, user
        req.save()
        notifications.notify(req, Action.COMPLETED)
        station_sync.service_request_changed(req, "completed")
    return req


# -- Rows ------------------------------------------------------------------------------


def _local(moment, zone):
    return moment.astimezone(zone).strftime(TIME_FORMAT) if moment else None


def _name(user):
    return (user.display_name or user.username) if user else None


def target_json(req):
    if req.vehicle_id:
        vehicle = req.vehicle
        return {"target": VEHICLE, "vehicle_id": vehicle.pk, "vehicle_identifier": vehicle.identifier,
                "vehicle_name": vehicle.vehicle_name, "asset_id": None, "asset_code": None, "asset_type": None}
    asset = req.asset
    return {"target": ASSET, "vehicle_id": None, "vehicle_identifier": None, "vehicle_name": None,
            "asset_id": asset.pk, "asset_code": asset.asset_code, "asset_type": asset.asset_type.asset_type_name}


def photo_fields(req, channel):
    """The photo for an app: its id and two server-relative URLs (they need
    the access token). None when the report carried no photo."""
    if not req.photo_id:
        return {"photo_id": None, "photo_url": None, "photo_thumb_url": None}
    return {"photo_id": str(req.photo_id),
            "photo_url": photo_services.view_path(channel, req.photo_id, "display"),
            "photo_thumb_url": photo_services.view_path(channel, req.photo_id, "thumb")}


def request_json(req, zone):
    """One request as the tablet sees it."""
    return {
        "request_sync_id": str(req.pk), "request_no": req.request_no, "status": req.status,
        **target_json(req), **issue_type_json(req), "issue": req.issue,
        "requested_by": _name(req.requested_by), "requested_at": _local(req.requested_at, zone),
        "decided_by": _name(req.decided_by), "decided_channel": req.decided_channel or None,
        "decided_at": _local(req.decided_at, zone), "decision_note": req.decision_note,
        "completed_by": _name(req.completed_by), "completed_at": _local(req.completed_at, zone),
        "completion_note": req.completion_note,
        "has_photo": bool(req.photo_id),
    }


def manager_json(req, zone):
    """A service request in the manager's combined requests list
    (apps/rental/requests.py): the shared row shape -- no order -- with the
    request itself in `details`."""
    return {
        "request_sync_id": str(req.pk), "order_id": None, "order_no": None, "kind": "service",
        "status": req.status, "reason": summary(req), "requested_at": _local(req.requested_at, zone),
        "discount_type": None, "discount_value": None,
        "decided_by": _name(req.decided_by), "decided_channel": req.decided_channel or None,
        "decided_at": _local(req.decided_at, zone), "decision_note": req.decision_note,
        "revoked_by": None, "revoked_channel": None, "revoked_at": None, "revoke_note": "",
        "applied_amount": None, "applied_at": None, "used_at": None,
        "branch_id": req.branch_id, "station": req.branch.name, "requested_by": _name(req.requested_by),
        "order": None,
        "details": {
            "request_no": req.request_no, **target_json(req), **issue_type_json(req), "issue": req.issue,
            **photo_fields(req, Channel.MANAGER),
            "completed_by": _name(req.completed_by), "completed_at": _local(req.completed_at, zone),
            "completion_note": req.completion_note,
        },
    }


def issue_type_json(req):
    return {"issue_type_id": req.issue_type_id, "issue_type": req.issue_type.name if req.issue_type_id else None,
            "needs_warehouse_service": req.needs_warehouse_service}


def summary(req):
    """"Brake failure -- squeals at speed": the type, then the words."""
    parts = [req.issue_type.name if req.issue_type_id else "", req.issue]
    return " -- ".join(part for part in parts if part)


def with_rows(rows):
    return rows.select_related("branch", "company", "vehicle", "asset__asset_type", "issue_type", "requested_by",
                               "decided_by", "completed_by")


def station_list(branch, *, device=None, status=None, from_date=None, to_date=None, zone=None, page=1):
    """(requests, page) of the station's service requests -- or only one
    tablet's (`mine`) -- newest first, 50 a page."""
    rows = ServiceRequest.objects.filter(branch=branch)
    if device is not None:
        rows = rows.filter(device=device)
    if status:
        rows = rows.filter(status=status)
    if zone is not None and from_date:
        rows = rows.filter(requested_at__gte=datetime.datetime.combine(from_date, datetime.time.min, tzinfo=zone))
    if zone is not None and to_date:
        rows = rows.filter(requested_at__lt=datetime.datetime.combine(
            to_date + datetime.timedelta(days=1), datetime.time.min, tzinfo=zone))
    page = Paginator(with_rows(rows).order_by("-requested_at", "-pk"), PAGE_SIZE).get_page(page)
    return list(page.object_list), page


def station_assets(branch):
    """The station's active, approved assets for the tablet, each with its
    open service request number (None when it has none)."""
    assets = list(Asset.objects.filter(branch=branch, company=branch.company, is_active=True,
                                       approval_status=ApprovalStatus.APPROVED)
                  .select_related("asset_type", "brand").order_by("asset_code"))
    reported = open_for_assets(a.pk for a in assets)
    from apps.fleet.asset_checks import last_checks

    checked = last_checks(a.pk for a in assets)
    zone = zone_for(branch.company)

    def last(asset):
        check = checked.get(asset.pk)
        return {"last_check_status": check.status if check else None,
                "last_checked_at": _local(check.checked_at, zone) if check else None}

    return [{
        "asset_id": a.pk, "asset_code": a.asset_code, "asset_type": a.asset_type.asset_type_name,
        "brand": a.brand.brand_name if a.brand_id else None, "serial_no": a.serial_no,
        "description": a.description, "open_request_no": reported.get(a.pk), **last(a),
    } for a in assets]

"""Asset checks: the operator app inspects an asset of its station -- a
photo (required), its condition (active / needs inspection), optional
remarks. Recorded only: no approval, no notification, no service request
(owner, 27 Oct 2026). The asset's condition is its latest check. Managers view
them with photos in the manager app; the web lists them (Inventory -> Asset
Checks).

New: legacy had no asset checks (RmsCheckList, a question list per station,
was never used -- 0 rows).
"""

import datetime

from django.core.paginator import Paginator
from django.db import IntegrityError, transaction

from apps.fleet.models import Asset, AssetCheck
from apps.photos import services as photo_services
from apps.photos.models import PhotoPurpose
from core.enums import Channel

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
PAGE_SIZE = 50


class CheckRefused(Exception):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def record(session, values):
    """A new check from the tablet. Returns (check, created): created is False
    when this sync_id was already stored (a resend)."""
    branch = session.branch
    company = branch.company
    sync_id = values["sync_id"]
    existing = AssetCheck.objects.filter(pk=sync_id).first()
    if existing is not None:
        if existing.company_id != company.pk:
            raise CheckRefused("sync_id_conflict", "That sync_id is already used.")
        return existing, False
    asset = Asset.objects.filter(pk=values["asset_id"], company=company, is_active=True).first()
    if asset is None:
        raise CheckRefused("unknown_asset", "No asset with that id.", 404)
    if asset.branch_id != branch.pk:
        raise CheckRefused("asset_not_at_station", "That asset is not at this station.", 400)
    with transaction.atomic():
        try:
            photo = photo_services.attachable(values["photo_id"], company, PhotoPurpose.ASSET_CHECK)
        except photo_services.PhotoRefused as refusal:
            raise CheckRefused(refusal.code, refusal.message, refusal.status) from None
        check = AssetCheck(
            id=sync_id, company=company, branch=branch, asset=asset, status=values["status"],
            remarks=(values.get("remarks") or "").strip(), photo=photo, device=session.device,
            checked_by=session.user, checked_at=values["checked_at"],
            created_by=session.user, modified_by=session.user,
        )
        try:
            with transaction.atomic():
                check.save(force_insert=True)
        except IntegrityError:
            raced = AssetCheck.objects.filter(pk=sync_id).first()
            if raced is None:
                raise
            return raced, False
        photo_services.mark_attached(photo)
    return check, True


def last_checks(asset_ids):
    """{asset_id: latest AssetCheck} -- the asset's condition now."""
    latest = {}
    for check in (AssetCheck.objects.filter(asset_id__in=list(asset_ids)).select_related("checked_by")
                  .order_by("asset_id", "-checked_at", "-created_on").distinct("asset_id")):
        latest[check.asset_id] = check
    return latest


def with_rows(rows):
    return rows.select_related("asset__asset_type", "branch__location__state", "checked_by", "device", "company")


def between(rows, zone, from_date=None, to_date=None):
    if from_date:
        rows = rows.filter(checked_at__gte=datetime.datetime.combine(from_date, datetime.time.min, tzinfo=zone))
    if to_date:
        rows = rows.filter(checked_at__lt=datetime.datetime.combine(
            to_date + datetime.timedelta(days=1), datetime.time.min, tzinfo=zone))
    return rows


def page_of(rows, page):
    page = Paginator(with_rows(rows).order_by("-checked_at", "-pk"), PAGE_SIZE).get_page(page)
    return list(page.object_list), page


def station_list(branch, *, device=None, status=None, from_date=None, to_date=None, zone=None, page=1):
    rows = AssetCheck.objects.filter(branch=branch)
    if device is not None:
        rows = rows.filter(device=device)
    if status:
        rows = rows.filter(status=status)
    return page_of(between(rows, zone, from_date, to_date) if zone else rows, page)


def company_list(company_ids, *, state_id=None, branch_id=None, asset_id=None, asset_type_id=None,
                 serial_no="", status=None, from_date=None, to_date=None, zone=None, page=1):
    rows = AssetCheck.objects.filter(company_id__in=company_ids)
    if state_id:
        rows = rows.filter(branch__location__state_id=state_id)
    if branch_id:
        rows = rows.filter(branch_id=branch_id)
    if asset_id:
        rows = rows.filter(asset_id=asset_id)
    if asset_type_id:
        rows = rows.filter(asset__asset_type_id=asset_type_id)
    if serial_no:                            # any part of the serial number, ignoring case
        rows = rows.filter(asset__serial_no__icontains=serial_no)
    if status:
        rows = rows.filter(status=status)
    return page_of(between(rows, zone, from_date, to_date) if zone else rows, page)


def _local(moment, zone):
    return moment.astimezone(zone).strftime(TIME_FORMAT) if moment else None


def check_json(check, zone, channel):
    """One check for an app. Only the manager app views photos: its rows carry
    two server-relative URLs (they need the access token)."""
    row = {
        "asset_check_id": str(check.pk), "asset_id": check.asset_id, "asset_code": check.asset.asset_code,
        "asset_type": check.asset.asset_type.asset_type_name, "status": check.status,
        "status_label": check.get_status_display(), "remarks": check.remarks,
        "branch_id": check.branch_id, "station": check.branch.name, "emirate": check.branch.location.state.name,
        "checked_by": (check.checked_by.display_name or check.checked_by.username) if check.checked_by_id else None,
        "checked_at": _local(check.checked_at, zone), "photo_id": str(check.photo_id),
    }
    if channel == Channel.MANAGER:
        row["photo_url"] = photo_services.view_path(channel, check.photo_id, "display")
        row["photo_thumb_url"] = photo_services.view_path(channel, check.photo_id, "thumb")
    return row

"""Image Sharing: a photo with remarks from the operator app, seen on the web
and in the manager app, deleted on the web. No approval -- nothing depends on
one (owner, 26 Oct 2026).

Legacy "MarketingIntelligence" (SFA_SaveMarketingIntelligence, a base64 photo
in SfaMarketingIntelligence.PhotoArray, approved/rejected/deleted on the web).
Kept: photo, remarks, station, who and when. Not kept: title, vehicle type,
vehicle no and address (the 2026 app specs show photo + remarks only), and the
approve/reject step. The photo now goes through apps/photos (compressed on the
device, stored as a file, previews), not into the database.
"""

import datetime

from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.fleet.models import ImageShare
from apps.photos import services as photo_services
from apps.photos.models import PhotoPurpose
from core.enums import Channel

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
PAGE_SIZE = 50


class ShareRefused(Exception):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def share(session, values):
    """A new image share from the tablet. Returns (share, created): created is
    False when this sync_id was already stored (a resend)."""
    branch = session.branch
    company = branch.company
    sync_id = values["sync_id"]
    existing = ImageShare.objects.filter(pk=sync_id).first()
    if existing is not None:
        if existing.company_id != company.pk:
            raise ShareRefused("sync_id_conflict", "That sync_id is already used.")
        return existing, False
    with transaction.atomic():
        try:
            photo = photo_services.attachable(values["photo_id"], company, PhotoPurpose.IMAGE_SHARE)
        except photo_services.PhotoRefused as refusal:
            raise ShareRefused(refusal.code, refusal.message, refusal.status) from None
        item = ImageShare(
            id=sync_id, company=company, branch=branch, device=session.device, shared_by=session.user,
            shared_at=values["shared_at"], remarks=values["remarks"].strip(), photo=photo,
            created_by=session.user, modified_by=session.user,
        )
        try:
            with transaction.atomic():
                item.save(force_insert=True)
        except IntegrityError:
            raced = ImageShare.objects.filter(pk=sync_id).first()
            if raced is None:
                raise
            return raced, False
        photo_services.mark_attached(photo)
    return item, True


def delete(user, share_id, company_ids):
    """Deleted on the web: kept, out of every list, its photo no longer served
    (apps/photos/services.py::_withdrawn)."""
    with transaction.atomic():
        item = (ImageShare.objects.select_for_update()
                .filter(pk=share_id, company_id__in=company_ids, deleted_at__isnull=True).first())
        if item is None:
            raise ShareRefused("unknown_share", "No image share with that id.", 404)
        item.deleted_at, item.deleted_by, item.modified_by = timezone.now(), user, user
        item.save(update_fields=["deleted_at", "deleted_by", "modified_by", "modified_on"])
    return item


def visible(rows):
    return rows.filter(deleted_at__isnull=True).select_related(
        "branch__location__state", "shared_by", "device", "company")


def between(rows, zone, from_date=None, to_date=None):
    if from_date:
        rows = rows.filter(shared_at__gte=datetime.datetime.combine(from_date, datetime.time.min, tzinfo=zone))
    if to_date:
        rows = rows.filter(shared_at__lt=datetime.datetime.combine(
            to_date + datetime.timedelta(days=1), datetime.time.min, tzinfo=zone))
    return rows


def _local(moment, zone):
    return moment.astimezone(zone).strftime(TIME_FORMAT) if moment else None


def share_json(item, zone, channel):
    """One share for an app. Only the manager app may view photos
    (apps/photos/services.py::can_view_app): its rows carry two
    server-relative URLs that need the access token; the tablet's carry none."""
    row = {
        "share_id": str(item.pk), "remarks": item.remarks,
        "branch_id": item.branch_id, "station": item.branch.name,
        "emirate": item.branch.location.state.name,
        "shared_by": (item.shared_by.display_name or item.shared_by.username) if item.shared_by_id else None,
        "shared_at": _local(item.shared_at, zone),
        "photo_id": str(item.photo_id),
    }
    if channel == Channel.MANAGER:
        row["photo_url"] = photo_services.view_path(channel, item.photo_id, "display")
        row["photo_thumb_url"] = photo_services.view_path(channel, item.photo_id, "thumb")
    return row


def page_of(rows, page):
    page = Paginator(rows.order_by("-shared_at", "-pk"), PAGE_SIZE).get_page(page)
    return list(page.object_list), page


def station_list(branch, *, device=None, from_date=None, to_date=None, zone=None, page=1):
    """(shares, page): the station's -- or one tablet's (`mine`) -- newest first."""
    rows = visible(ImageShare.objects.filter(branch=branch))
    if device is not None:
        rows = rows.filter(device=device)
    return page_of(between(rows, zone, from_date, to_date) if zone else rows, page)


def manager_list(company_ids, *, state_id=None, branch_id=None, from_date=None, to_date=None, zone=None,
                 page=1):
    """(shares, page) across the companies, newest first -- view only."""
    rows = visible(ImageShare.objects.filter(company_id__in=company_ids))
    if state_id:
        rows = rows.filter(branch__location__state_id=state_id)
    if branch_id:
        rows = rows.filter(branch_id=branch_id)
    return page_of(between(rows, zone, from_date, to_date) if zone else rows, page)

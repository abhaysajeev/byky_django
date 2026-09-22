"""Device rules that more than one caller needs.

Mostly read-only. The writes are device registration (the tablet's own call)
and the Device Approval actions -- approve, reconnect, reject, block, unblock --
each one transaction that re-checks the device's status under a row lock, so a
double click or two admins can never act twice.

The app-update lookup (design/03-login.md section 9A) is used by three callers:
the pre-login update check, login's step 0, and the App Releases screen's
preview. Keeping it in one function is what stops them ever disagreeing.

format_bill_number is the one place a printed receipt number is built. The
legacy built it on the tablet, out of reach of the server that had to check it.
"""

import base64
import logging
import re
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction
from django.db.models import Case, F, IntegerField, Max, Q, Value, When
from django.utils import timezone

from apps.company import services as company_services
from apps.company.models import Branch, BranchWorkingTime, Company
from apps.devices.models import (
    BILL_NUMBER_WIDTH,
    VERSION_NAME,
    AppRelease,
    AppReleaseMapping,
    BillContinuity,
    BillKind,
    Device,
    DeviceAction,
    DeviceMapping,
    DeviceSettings,
    DeviceStatus,
    DeviceStatusLog,
    ROUND_OFF_STEPS,
    PrintType,
    ReleaseScope,
    RoundOffMode,
    UpdateType,
)
from apps.portal.auth import close_sessions_for_device
from apps.portal.session_models import AppSession, LogoutReason
from core.enums import ApprovalStatus, Channel

log = logging.getLogger(__name__)

NO_UPDATE = {"update_required": False}


# -- Which build a device should be on --------------------------------------


def release_for(*, company_id, channel, branch_id=None):
    """The release a device at `branch_id` should run, or None.

    Most specific first: the branch's own mapping, then the company's
    All-branches mapping. The unique index allows at most one active row at
    each level, so there is never a tie to break; the version code in the
    ordering only keeps the result deterministic.

    `release__is_active` sits in the same query on purpose. A withdrawn release
    must fall through to the All-branches row -- testing it after fetching the
    branch mapping would answer "nothing" instead, and strand every branch
    mapped to the withdrawn build.
    """
    scopes = Q(scope=ReleaseScope.ALL)
    if branch_id is not None:
        scopes |= Q(scope=ReleaseScope.BRANCH, branch_id=branch_id)

    mapping = (
        AppReleaseMapping.objects
        .filter(company_id=company_id, channel=channel, is_active=True, release__is_active=True)
        .filter(scopes)
        .annotate(specificity=Case(
            When(scope=ReleaseScope.BRANCH, then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        ))
        .select_related("release")
        .order_by("specificity", "-release__version_code", "-id")
        .first()
    )
    return mapping.release if mapping else None


_NUMBERS = re.compile(r"\d+(?:\.\d+)*")


def _version_parts(name):
    """'1.12.80' -> (1, 12, 80). Tolerates a prefix such as 'Version ' -- seven
    variants of it appear in the legacy's login records. None if there are no
    digits at all."""
    match = _NUMBERS.search(name or "")
    if not match:
        return None
    return tuple(int(part) for part in match.group(0).split("."))


def _is_behind(installed, target):
    """Compare version tuples part by part, a missing part counting as 0 --
    so 1.13 is 1.13.0, and 1.12.10 is newer than 1.12.9."""
    width = max(len(installed), len(target))
    pad = lambda parts: parts + (0,) * (width - len(parts))  # noqa: E731
    return pad(installed) < pad(target)


def update_decision(*, company_id, channel, branch_id=None, version_code=None, version_name=""):
    """What to tell a device running `version_code` / `version_name`.

    Never a downgrade: a device already on the release, or newer, is told
    there is nothing to do. `is_mandatory` and the release's details are only
    ever sent alongside `update_required: true` -- a reply saying "no update"
    but "mandatory" would be a trap for the app.

    Fails open. An unreadable version, or any error at all, answers "no
    update": a version check must never be what stops every station working.
    Login's step 0 runs this same function, so a mandatory update the check
    could not decide is still caught there.
    """
    try:
        release = release_for(company_id=company_id, channel=channel, branch_id=branch_id)
        if release is None:
            return dict(NO_UPDATE)

        if version_code is not None:
            behind = version_code < release.version_code
        else:
            installed = _version_parts(version_name)
            target = _version_parts(release.version_name)
            if installed is None or target is None:
                log.warning("update check: unreadable version %r", version_name)
                return dict(NO_UPDATE)
            behind = _is_behind(installed, target)

        if not behind:
            return dict(NO_UPDATE)

        return {
            "update_required": True,
            "is_mandatory": release.is_mandatory,
            "version_code": release.version_code,
            "version_name": release.version_name,
            "update_note": release.update_note,
            "download_url": release.download_url,
        }
    except Exception:
        log.exception("update check failed; answering no update")
        return dict(NO_UPDATE)


def deployment_company_id():
    """The company an unknown device belongs to: this deployment's one company.

    A device that has never registered has no company on record, and the app
    is never trusted to name one. With exactly one active company there is
    nothing to choose; with none or several there is no safe answer, so the
    unknown device gets no release.
    """
    ids = list(Company.objects.filter(is_active=True).values_list("id", flat=True)[:2])
    return ids[0] if len(ids) == 1 else None


def company_for_code(code):
    """The company an app names at registration, matched on `Company.short_code`.

    Multi-company is the deployment shape: one server, several companies (BYKY
    and others like it). A tablet therefore has to say which one it belongs to,
    because before approval it has no user, no station and nothing else that
    could say it -- design/03-login.md §10 item 9 kept this door open for
    exactly this case.

    The value is taken as sent and matched, not verified: the client's licence
    server issues it, and **approval is the control that matters**. A tablet
    naming the wrong company lands in that company's pending queue, where an
    admin who does not recognise it rejects it; until then it has no station, no
    login and no data.

    `short_code` is text, so a numeric licence id (`1`, `1001`) matches as
    readily as `BYKY`.
    """
    code = str(code or "").strip()
    if not code:
        return None
    return (
        Company.objects.filter(is_active=True, short_code__iexact=code)
        .values_list("id", flat=True).first()
    )


def company_for_registration(code):
    """(company_id, refusal): the company a new device belongs to.

    A code that names an active company wins. With no code, a single-company
    deployment still works exactly as before -- which is what every existing
    tablet and every test sends.
    """
    if code:
        company_id = company_for_code(code)
        return (company_id, None) if company_id else (None, UNKNOWN_COMPANY)
    company_id = deployment_company_id()
    return (company_id, None) if company_id else (None, UNAVAILABLE)


def update_decision_for_installation(
    *, installation_id, channel, version_code=None, version_name="", company_code="", at=None
):
    """update_decision for a tablet identified only by its installation id.

    The branch is looked up, never sent: installation id -> device -> its open
    station mapping. A device the server does not know yet -- first launch, a
    reinstall awaiting reconnection -- or one not yet placed at a station gets
    the All-branches release. Device status is not consulted: a pending tablet
    must still be able to update, or a new one on an old build could never
    reach login to register.

    An "Any time" release is offered only while the branch is open
    (section 9A.3a); outside its hours, or with no hours set, the answer is
    "no update" plus a `reason` the API turns into its code. A mandatory
    release is never held back.
    """
    try:
        company_id, branch_id = _company_and_branch(installation_id, channel, company_code)
    except Exception:
        # Fails open like update_decision: section 9A.7 row 13.
        log.exception("update check: device lookup failed; answering no update")
        return dict(NO_UPDATE)

    if company_id is None:
        return dict(NO_UPDATE)
    decision = update_decision(
        company_id=company_id, channel=channel, branch_id=branch_id,
        version_code=version_code, version_name=version_name,
    )
    if decision.get("update_required") and not decision.get("is_mandatory"):
        held = _outside_working_time(branch_id, at)
        if held:
            return {**NO_UPDATE, "reason": held}
    return decision


# Why an "Any time" release was held back; the API uses these as its codes.
OUTSIDE_WORKING_HOURS = "outside_working_hours"
WORKING_TIME_NOT_SET = "working_time_not_set"


def _outside_working_time(branch_id, at=None):
    """None while the branch is open, else the reason to hold the release.

    A tablet with no branch has no hours to go by -- not set. Fails open: an
    error reading the hours offers the release, because holding back an
    optional update is never worth a broken check.
    """
    if branch_id is None:
        return WORKING_TIME_NOT_SET
    try:
        branch = Branch.objects.select_related("company").get(pk=branch_id)
        state = company_services.branch_open_state(branch, at)
    except Exception:
        log.exception("update check: working time lookup failed; offering the update")
        return None
    if state == company_services.NOT_SET:
        return WORKING_TIME_NOT_SET
    if state == company_services.CLOSED:
        return OUTSIDE_WORKING_HOURS
    return None


def _company_and_branch(installation_id, channel, company_code=""):
    """(company_id, branch_id) for an installation; branch None if unplaced."""
    company_id = None
    branch_id = None
    device = (
        Device.objects
        .filter(installation_id=installation_id, channel=channel)
        .only("id", "company_id")
        .first()
    )
    if device is not None:
        company_id = device.company_id
        branch_id = (
            DeviceMapping.objects
            .filter(device_id=device.id, to_date__isnull=True)
            .values_list("branch_id", flat=True)
            .first()
        )
    else:
        # An unregistered tablet has no row to read a company from, so it names
        # one. Without that, a new tablet on an old build in a multi-company
        # deployment could never be told about a mandatory release.
        company_id = company_for_code(company_code) or deployment_company_id()
    return company_id, branch_id


# -- Registration: the tablet's step 2 (design/03-login.md section 9B) -------

# Outcomes of register_device; the API turns each into its code and HTTP status.
APPROVED = "approved"
PENDING = "pending_approval"
RECONNECT_PENDING = "reconnect_pending"
BLOCKED = "device_blocked"
RETIRED = "device_retired"
APP_MISMATCH = "app_mismatch"
UNAVAILABLE = "registration_unavailable"
UNKNOWN_COMPANY = "unknown_company"

PUSH_TOKEN_MAX = 512


def _push_token(value):
    """The push token from the credentials block, or "" to leave it alone.
    Anything odd is ignored: credentials never fail a call."""
    if isinstance(value, str) and 0 < len(value.strip()) <= PUSH_TOKEN_MAX:
        return value.strip()
    return ""


def _outcome_of(device, channel):
    """What a known installation is told, from its own row only."""
    if device.channel != channel:
        return APP_MISMATCH
    if device.status == DeviceStatus.RETIRED:
        return RETIRED
    if device.status == DeviceStatus.BLOCKED:
        return BLOCKED
    if device.status == DeviceStatus.PENDING:
        return RECONNECT_PENDING if device.reconnect_of_id else PENDING
    return APPROVED


def _refresh_known(device, *, channel, platform_id, device_model, push_token, now):
    """The quiet updates a known device gets on every call.

    platform_id changes only on an approved device -- the re-signed-app case.
    On a pending one it would let a caller rewrite the reinstall hint of a
    registration nobody has vouched for yet.
    """
    changed = {"last_seen_at"}
    device.last_seen_at = now
    if device.channel == channel and device.status != DeviceStatus.RETIRED:
        if push_token and push_token != device.push_token:
            device.push_token = push_token
            changed.add("push_token")
        if device_model and device_model != device.device_model:
            device.device_model = device_model
            changed.add("device_model")
        if (device.status == DeviceStatus.APPROVED and platform_id
                and platform_id != device.platform_id):
            device.platform_id = platform_id
            changed.add("platform_id")
    device.save(update_fields=sorted(changed | {"modified_on"}))


def _reinstall_match(*, company_id, channel, platform, platform_id):
    """The device a new installation appears to be, or None.

    A hint for the admin, never an identity (section 9B.3): the platform id is a
    value the app sends. Retired rows and other reconnect requests never match.
    """
    if not platform_id:
        return None
    return (
        Device.objects
        .filter(
            company_id=company_id, channel=channel, platform=platform,
            platform_id=platform_id, reconnect_of__isnull=True,
        )
        .exclude(status=DeviceStatus.RETIRED)
        .order_by(F("last_seen_at").desc(nulls_last=True), "-id")
        .first()
    )


def device_for_installation(installation_id):
    """The Device row a request's installation_id names, or None. Public --
    used by registration (below) and by operator login (apps/portal/auth.py)
    to find which till a request is about."""
    return Device.objects.filter(installation_id=installation_id).first()


def register_device(*, installation_id, channel, platform, platform_id="",
                    device_model="", push_token=None, company_code="", now=None):
    """(outcome, device) for one registration call. Idempotent.

    1. A known installation is answered from its own row.
    2. An unknown one whose platform id matches a live device of this app gets a
       separate pending request pointing at it. The matched row is not touched:
       a forged platform id must not be able to take a working station offline.
    3. Anything else is a new pending device, numbered at once so the waiting
       screen can show it.

    `company_code` is the company the app names (§9B.2): matched on
    `Company.short_code`, taken as sent. A known installation is answered from
    its own row and never re-homed -- a device belongs to the company that
    approved it, and a later call naming another one cannot move it.
    """
    now = now or timezone.now()
    platform_id = (platform_id or "").strip()
    device_model = (device_model or "").strip()
    token = _push_token(push_token)

    known = device_for_installation(installation_id)
    if known is not None:
        _refresh_known(known, channel=channel, platform_id=platform_id,
                       device_model=device_model, push_token=token, now=now)
        return _outcome_of(known, channel), known

    company_id, refusal = company_for_registration(company_code)
    if company_id is None:
        log.error("registration: no company for device %s (code %r)",
                  installation_id, company_code)
        return refusal, None

    match = _reinstall_match(company_id=company_id, channel=channel,
                             platform=platform, platform_id=platform_id)
    try:
        with transaction.atomic():
            device = Device.objects.create(
                company_id=company_id, installation_id=installation_id,
                channel=channel, platform=platform, platform_id=platform_id,
                device_model=device_model, push_token=token,
                status=DeviceStatus.PENDING, reconnect_of=match, last_seen_at=now,
            )
    except IntegrityError:
        # Two first calls raced; the other one created the row. Answer from it.
        device = Device.objects.get(installation_id=installation_id)
    return _outcome_of(device, channel), device


# -- Device Approval actions (design/03-login.md sections 9B.3-9B.5) ---------


class DeviceActionError(Exception):
    """A refused admin action, with the line the screen shows."""

    def __init__(self, message, field=""):
        super().__init__(message)
        self.message = message
        self.field = field


NAME_MAX = 120


def _locked(devices_qs, pk):
    """The device, row-locked, from the admin's own scoped queryset."""
    device = devices_qs.select_for_update().filter(pk=pk).first()
    if device is None:
        raise DeviceActionError("That device was not found.")
    return device


def _log(device, action, from_status, to_status, user, *, reason="", remarks="", related=None):
    DeviceStatusLog.objects.create(
        device=device, action=action, from_status=from_status, to_status=to_status,
        reason=(reason or "")[:100], remarks=remarks or "", related_device=related,
        created_by=user,
    )


def _open_mapping(device):
    return DeviceMapping.objects.select_for_update().filter(
        device=device, to_date__isnull=True,
    ).first()


def _close_mapping(device, now, user=None):
    """End the device's open mapping at `now`. Never before it began."""
    mapping = _open_mapping(device)
    if mapping is not None:
        mapping.to_date = max(now, mapping.from_date)
        mapping.modified_by = user
        mapping.save(update_fields=["to_date", "modified_by", "modified_on"])
    return mapping


def _check_station(branch, device, *, excluding=None):
    """The station may take this device: same company, active, and -- unless the
    branch allows several -- not already holding another approved device. The
    branch row is locked so two approvals at one station cannot both pass."""
    if branch.company_id != device.company_id or not branch.is_active:
        raise DeviceActionError("Choose an active station of this company.", "Station")
    Branch.objects.select_for_update().filter(pk=branch.pk).first()
    if branch.is_multi_device:
        return
    others = (
        DeviceMapping.objects
        .filter(branch=branch, to_date__isnull=True, device__status=DeviceStatus.APPROVED)
        .exclude(device=device)
    )
    if excluding is not None:
        others = others.exclude(device=excluding)
    holder = others.select_related("device").first()
    if holder is not None:
        raise DeviceActionError(
            f"{branch.name} already has {holder.device} and allows one device. "
            "Replace that device, or allow several on the branch.",
            "Station",
        )


def approve_device(devices_qs, pk, *, user, name, branch=None, replaces_pk=None, now=None):
    """Approve a pending registration: name it and, optionally, place it; when
    it stands in for an older tablet, retire that one (section 9B.4).

    The station is optional for every app: stations are Device Mapping's job,
    and an approved operator device with none simply cannot log in until it is
    mapped (login step 9, device_not_mapped).

    On a reconnect request this is "register as new": the link to the device it
    looked like is dropped and it becomes a device in its own right.
    """
    now = now or timezone.now()
    name = (name or "").strip()
    if not name:
        raise DeviceActionError("Enter a name for the device.", "Name")
    if len(name) > NAME_MAX:
        raise DeviceActionError(f"Keep the name to {NAME_MAX} characters.", "Name")

    with transaction.atomic():
        device = _locked(devices_qs, pk)
        if device.status != DeviceStatus.PENDING:
            raise DeviceActionError("Only a device awaiting approval can be approved.")

        old = None
        if replaces_pk:
            old = _locked(devices_qs, replaces_pk)
            if (old.pk == device.pk or old.channel != device.channel
                    or old.status not in (DeviceStatus.APPROVED, DeviceStatus.BLOCKED)):
                raise DeviceActionError(
                    "The device being replaced must be an approved or blocked device of the same app.",
                    "Replaces",
                )

        if branch is not None:
            _check_station(branch, device, excluding=old)

        if old is not None:
            old_status = old.status
            _close_mapping(old, now, user)
            close_sessions_for_device(old, LogoutReason.FORCED, now=now)
            old.status = DeviceStatus.RETIRED
            old.save(update_fields=["status", "modified_on"])
            _log(old, DeviceAction.RETIRED, old_status, DeviceStatus.RETIRED, user,
                 remarks=f"Replaced by device {device.device_registration_id}.", related=device)

        device.status = DeviceStatus.APPROVED
        device.name = name
        device.approved_by = user
        device.approved_at = now
        device.reconnect_of = None
        device.replaced_device = old
        device.modified_by = user
        device.save(update_fields=[
            "status", "name", "approved_by", "approved_at", "reconnect_of",
            "replaced_device", "modified_by", "modified_on",
        ])
        if branch is not None:
            DeviceMapping.objects.create(device=device, branch=branch, from_date=now, created_by=user)

        if old is not None:
            _log(device, DeviceAction.REPLACED, DeviceStatus.PENDING, DeviceStatus.APPROVED, user,
                 remarks=f"Replaces device {old.device_registration_id}.", related=old)
        else:
            _log(device, DeviceAction.APPROVED, DeviceStatus.PENDING, DeviceStatus.APPROVED, user)
    return device


def reconnect_device(devices_qs, pk, *, user, now=None):
    """Confirm a reinstall (section 9B.3): the request's new installation moves
    onto the original device, which keeps its number, name, station, status and
    history. The request row is discarded.

    The original's open sessions are closed: they belonged to the app install
    that no longer exists.
    """
    now = now or timezone.now()
    with transaction.atomic():
        request_row = _locked(devices_qs, pk)
        if request_row.status != DeviceStatus.PENDING or not request_row.reconnect_of_id:
            raise DeviceActionError("This is not a reinstall waiting to be reconnected.")
        original = _locked(devices_qs, request_row.reconnect_of_id)
        if original.status == DeviceStatus.RETIRED:
            raise DeviceActionError(
                "The device this looked like has been retired. Register it as new instead."
            )

        moved = {
            "installation_id": request_row.installation_id,
            "platform_id": request_row.platform_id,
            "device_model": request_row.device_model or original.device_model,
            "push_token": request_row.push_token or original.push_token,
            "last_seen_at": request_row.last_seen_at or original.last_seen_at,
        }
        request_number = request_row.device_registration_id
        # Deleted first: installation_id is unique, and the original takes it.
        request_row.delete()

        close_sessions_for_device(original, LogoutReason.FORCED, now=now)
        for field, value in moved.items():
            setattr(original, field, value)
        original.modified_by = user
        original.save(update_fields=[*moved, "modified_by", "modified_on"])
        _log(original, DeviceAction.RECONNECTED, original.status, original.status, user,
             remarks=f"Reinstall confirmed; request {request_number} discarded.")
    return original


def reject_device(devices_qs, pk, *, user, reason=""):
    """A registration that should not exist. The tablet is then told
    device_retired, the one reply that means "register again"."""
    with transaction.atomic():
        device = _locked(devices_qs, pk)
        if device.status != DeviceStatus.PENDING:
            raise DeviceActionError("Only a device awaiting approval can be rejected.")
        device.status = DeviceStatus.RETIRED
        device.modified_by = user
        device.save(update_fields=["status", "modified_by", "modified_on"])
        _log(device, DeviceAction.REJECTED, DeviceStatus.PENDING, DeviceStatus.RETIRED, user,
             reason=reason)
    return device


def block_device(devices_qs, pk, *, user, reason="", now=None):
    """Stop an approved device at once: its open sessions end now, and its
    station stays recorded so an unblock puts it back where it was."""
    now = now or timezone.now()
    with transaction.atomic():
        device = _locked(devices_qs, pk)
        if device.status != DeviceStatus.APPROVED:
            raise DeviceActionError("Only an approved device can be blocked.")
        device.status = DeviceStatus.BLOCKED
        device.modified_by = user
        device.save(update_fields=["status", "modified_by", "modified_on"])
        close_sessions_for_device(device, LogoutReason.DEVICE_BLOCKED, now=now)
        _log(device, DeviceAction.BLOCKED, DeviceStatus.APPROVED, DeviceStatus.BLOCKED, user,
             reason=reason)
    return device


def unblock_device(devices_qs, pk, *, user, reason=""):
    """Back to approved. A device with no station stays unable to log in until
    Device Mapping gives it one -- as after an approval without a station."""
    with transaction.atomic():
        device = _locked(devices_qs, pk)
        if device.status != DeviceStatus.BLOCKED:
            raise DeviceActionError("Only a blocked device can be unblocked.")
        device.status = DeviceStatus.APPROVED
        device.modified_by = user
        device.save(update_fields=["status", "modified_by", "modified_on"])
        _log(device, DeviceAction.UNBLOCKED, DeviceStatus.BLOCKED, DeviceStatus.APPROVED, user,
             reason=reason)
    return device


# -- Device Mapping: which station a device trades at (section 8.6) ----------
#
# Nobody types a date: mapping stamps now, and a re-map or close stamps the old
# row's to_date with the same moment -- what the legacy did
# (DeviceMappingModels.cs:235, DeviceMappingController.cs:315). Intervals are
# half-open, [from_date, to_date), and a re-map closes the open row and opens a
# new one, so the mapping table is its own history. All three are refused while
# anyone is logged in on the device, as the legacy refused an "Active Status"
# edit: moving a tablet mid-shift would put that shift's money at two stations.


def _in_use(device):
    return AppSession.objects.filter(device=device, logged_out_at__isnull=True).exists()


def _mappable(devices_qs, pk):
    device = _locked(devices_qs, pk)
    if device.status not in (DeviceStatus.APPROVED, DeviceStatus.BLOCKED):
        raise DeviceActionError("Only an approved or blocked device can be mapped.", "Device")
    if _in_use(device):
        raise DeviceActionError(
            "Someone is logged in on this device. Log them out first.", "Device"
        )
    return device


def map_device(devices_qs, pk, *, user, branch, now=None):
    """Give a device with no station one, from now."""
    now = now or timezone.now()
    with transaction.atomic():
        device = _mappable(devices_qs, pk)
        if _open_mapping(device) is not None:
            raise DeviceActionError("This device already has a station. Use Re-map.", "Device")
        _check_station(branch, device)
        return DeviceMapping.objects.create(
            device=device, branch=branch, from_date=now, created_by=user,
        )


def remap_device(devices_qs, pk, *, user, branch, now=None):
    """Move a device to another station now: the open row ends at the moment
    the new one begins."""
    now = now or timezone.now()
    with transaction.atomic():
        device = _mappable(devices_qs, pk)
        current = _open_mapping(device)
        if current is None:
            raise DeviceActionError("This device has no station yet. Use Add Device Mapping.", "Device")
        if current.branch_id == branch.pk:
            raise DeviceActionError("The device is already at this station.", "Station")
        _check_station(branch, device)
        _close_mapping(device, now, user)
        return DeviceMapping.objects.create(
            device=device, branch=branch, from_date=now, created_by=user,
        )


def close_mapping(devices_qs, pk, *, user, now=None):
    """End the device's mapping now. An operator device without a station
    cannot log in (login step 9) until it is mapped again."""
    now = now or timezone.now()
    with transaction.atomic():
        device = _mappable(devices_qs, pk)
        if _open_mapping(device) is None:
            raise DeviceActionError("This device has no station to close.", "Device")
        return _close_mapping(device, now, user)


# -- App Releases and App Mapping (design/03-login.md sections 8.5, 8.5a, 9A.6) --
#
# What a tablet is told comes from release_for above; everything here only
# writes the rows it reads. The screens' preview and grid call the same
# resolution rules, so an admin never sees one build and a tablet another.
# Promised to the app team in design/registration/update-check-api.md: the
# integer code is compared (section 6.1), and a release whose code is not
# higher than the last is refused (section 7.1).

APP_CHANNELS = (Channel.OPERATOR, Channel.MANAGER, Channel.EMPLOYEE)
URL_MAX = 500
VERSION_CODE_MAX = 2147483647    # PositiveIntegerField's ceiling in Postgres
_URL = URLValidator()


class Invalid(Exception):
    """Every problem with a posted form at once, as {field, message} pairs, so a
    drawer can list them together instead of one per save."""

    def __init__(self, errors):
        super().__init__("; ".join(e["message"] for e in errors))
        self.errors = errors


# The name the release screens were written against.
ReleaseInvalid = Invalid


def app_label(channel):
    return Channel(channel).label


def person_name(user):
    return (user.display_name or user.username) if user else ""


def _release_label(release):
    return f"{release.version_name} ({release.version_code})"


def _branches(n, singular="", plural=""):
    """'1 branch moves' / '3 branches move': the count, the noun, and the
    verb that agrees with them."""
    return f"{n} branch{'es' if n != 1 else ''}" + (f" {singular if n == 1 else plural}" if singular else "")


def _check_editable(update_type, download_url):
    """The two fields both Add and Edit own. No https rule: any well-formed
    link is accepted, by the client's decision (19 Sep 2026)."""
    errors = []
    if update_type not in UpdateType.values:
        errors.append({"field": "Update type", "message": "Choose Mandatory or Any time."})
    url = (download_url or "").strip()
    if not url:
        errors.append({"field": "Download link", "message": "Enter the download link."})
    elif len(url) > URL_MAX:
        errors.append({"field": "Download link", "message": f"Keep the link under {URL_MAX} characters."})
    else:
        try:
            _URL(url)
        except ValidationError:
            errors.append({"field": "Download link", "message": "Enter a full link, like http://files.example/app.apk."})
    return errors, url


def create_release(*, user, company_id, channel, version_name, version_code,
                   update_type, download_url, update_note=""):
    """Add a build to the catalogue. It reaches no tablet until it is mapped."""
    errors = []
    if channel not in APP_CHANNELS:
        errors.append({"field": "App", "message": "Choose the app this build is for."})

    name = (version_name or "").strip()
    try:
        VERSION_NAME(name)
        if len(name) > 20:
            raise ValidationError("too long")
    except ValidationError:
        errors.append({"field": "Version name", "message": "Use three numbers with dots, like 1.12.81."})

    try:
        code = int(str(version_code).strip())
    except (TypeError, ValueError):
        code = None
    if code is None or not 0 < code <= VERSION_CODE_MAX:
        errors.append({"field": "Version code", "message": "Enter the build's version code, a whole number above 0."})

    more, url = _check_editable(update_type, download_url)
    errors += more
    if errors:
        raise ReleaseInvalid(errors)

    with transaction.atomic():
        # One release at a time per company: "higher than the last" is only
        # true if nobody else is adding one between the read and the insert.
        Company.objects.select_for_update().filter(pk=company_id).first()
        same_app = AppRelease.objects.filter(company_id=company_id, channel=channel)
        app = app_label(channel)

        latest = same_app.order_by("-version_code").first()
        if latest is not None and code <= latest.version_code:
            errors.append({
                "field": "Version code",
                "message": f"Must be higher than {latest.version_code}, the code of {app} "
                           f"{latest.version_name}. Tablets compare this number, so it only goes up.",
            })
        if same_app.filter(version_name=name).exists():
            errors.append({"field": "Version name", "message": f"{name} is already a {app} release."})
        if errors:
            raise ReleaseInvalid(errors)

        return AppRelease.objects.create(
            company_id=company_id, channel=channel, version_name=name, version_code=code,
            update_type=update_type, download_url=url,
            update_note=(update_note or "").strip(), created_by=user,
        )


def _locked_release(releases_qs, pk):
    release = releases_qs.select_for_update().filter(pk=pk).first()
    if release is None:
        raise DeviceActionError("That release was not found.")
    return release


def update_release(releases_qs, pk, *, user, update_type, download_url, update_note=""):
    """Change what a build says about itself. App, version name and code are
    fixed once saved: tablets compare against them, and a changed code would
    silently re-decide every tablet mapped to it."""
    errors, url = _check_editable(update_type, download_url)
    if errors:
        raise ReleaseInvalid(errors)
    with transaction.atomic():
        release = _locked_release(releases_qs, pk)
        release.update_type = update_type
        release.download_url = url
        release.update_note = (update_note or "").strip()
        release.modified_by = user
        release.save(update_fields=["update_type", "download_url", "update_note", "modified_by", "modified_on"])
    return release


def _set_release_active(releases_qs, pk, *, user, active):
    with transaction.atomic():
        release = _locked_release(releases_qs, pk)
        if release.is_active == active:
            state = "active" if active else "withdrawn"
            raise DeviceActionError(f"{release} is already {state}.")
        release.is_active = active
        release.modified_by = user
        release.save(update_fields=["is_active", "modified_by", "modified_on"])
    return release


def withdraw_release(releases_qs, pk, *, user):
    """Stop offering a build. Its mappings stay; the branches on it fall back
    to the All-branches release, exactly as release_for skips it."""
    return _set_release_active(releases_qs, pk, user=user, active=False)


def restore_release(releases_qs, pk, *, user):
    return _set_release_active(releases_qs, pk, user=user, active=True)


def withdraw_consequence(release):
    """The sentence the Withdraw confirm shows: where this build's branches go."""
    mappings = list(release.mappings.filter(is_active=True))
    if not mappings:
        return "No branch is on this build, so no tablet is affected."
    parts = []
    own = [m for m in mappings if m.scope == ReleaseScope.BRANCH]
    if own:
        fallback = (
            AppReleaseMapping.objects
            .filter(company_id=release.company_id, channel=release.channel, scope=ReleaseScope.ALL,
                    is_active=True, release__is_active=True)
            .exclude(release=release).select_related("release").first()
        )
        where = (
            f"back to {_release_label(fallback.release)}, the All-branches release"
            if fallback else "to no update"
        )
        parts.append(f"{_branches(len(own), 'mapped to it falls', 'mapped to it fall')} {where}.")
    if any(m.scope == ReleaseScope.ALL for m in mappings):
        parts.append("It is the All-branches release, so branches without their own mapping will be offered no update.")
    return " ".join(parts)


def working_time_branch_ids(branch_ids):
    """Which of these branches have any working time -- an Any time build is
    held back from the rest (update-check-api.md section 4, working_time_not_set)."""
    return set(
        BranchWorkingTime.objects
        .filter(branch_id__in=branch_ids, is_active=True)
        .values_list("branch_id", flat=True).distinct()
    )


def branch_release_state(company_id, channel, branches):
    """(all_mapping, rows): what each branch is offered now, and why.

    One pass over the active mappings, mirroring release_for: a branch's own
    mapping on an active release wins, otherwise the All row on an active
    release, otherwise nothing. `branches` is the caller's scoped list.
    """
    active = (
        AppReleaseMapping.objects
        .filter(company_id=company_id, channel=channel, is_active=True)
        .select_related("release", "created_by")
    )
    all_row = None
    own = {}
    for m in active:
        if m.scope == ReleaseScope.ALL:
            all_row = m
        else:
            own[m.branch_id] = m
    fallback = all_row if all_row is not None and all_row.release.is_active else None
    with_hours = working_time_branch_ids([b.pk for b in branches])

    rows = []
    for b in branches:
        mapping = own.get(b.pk)
        if mapping is not None and mapping.release.is_active:
            source, effective = "own", mapping
        elif fallback is not None:
            source, effective = "all", fallback
        else:
            source, effective = "none", None
        rows.append({
            "branch": b,
            "source": source,
            "mapping": effective,
            "release": effective.release if effective else None,
            # A branch mapped to a withdrawn build: shown, since it is why the
            # branch is on the fallback.
            "own_withdrawn": mapping.release if mapping is not None and not mapping.release.is_active else None,
            "own_mapping": mapping,
            "own_by": person_name(mapping.created_by) if mapping is not None else "",
            "has_working_time": b.pk in with_hours,
        })
    return all_row, rows


def mapping_plan(company_id, channel, release, scope, branches, targets):
    """What saving this mapping would change, and what the admin must confirm.

    `branches` is every active branch in scope; `targets` the ticked ones
    (ignored for All). Returns counts, a one-line summary and the warnings:
    an older build than All (update-check-api.md section 6.2's no-downgrade
    intent), a Mandatory build (tablets cannot log in until they install
    it), and an Any time build at branches with no working time (held back,
    section 4).
    """
    all_row, rows = branch_release_state(company_id, channel, branches)
    current_all = all_row.release if all_row is not None and all_row.release.is_active else None
    label = _release_label(release)

    if scope == ReleaseScope.ALL:
        moving = [r for r in rows if r["source"] != "own" and r["release"] != release]
        own = [r for r in rows if r["source"] == "own"]
        staying = len(rows) - len(moving) - len(own)
        summary = f"{_branches(len(moving), 'moves', 'move')} to {label}"
        if own:
            summary += f" · {_branches(len(own), 'keeps its', 'keep their')} own mapping"
        if staying:
            summary += f" · {staying} already on it"
        older = [r for r in own if r["release"].version_code < release.version_code]
        older_text = (
            f"{_branches(len(older), 'keeps its', 'keep their')} own mapping on an older build "
            f"than {label}: {', '.join(r['branch'].name for r in older[:5])}{'…' if len(older) > 5 else ''}."
        ) if older else ""
    else:
        wanted = {b.pk for b in targets}
        moving = [r for r in rows if r["branch"].pk in wanted and r["release"] != release]
        staying = len(rows) - len(moving)
        summary = f"{_branches(len(moving), 'moves', 'move')} to {label}"
        rest = staying
        if rest:
            summary += f" · {rest} unchanged" + (f" (All branches: {_release_label(current_all)})" if current_all else "")
        older = [r for r in moving if current_all is not None and release.version_code < current_all.version_code]
        older_text = (
            f"{_branches(len(older))} would get an older build than the "
            f"All-branches release {_release_label(current_all)}. Tablets already on the newer build "
            "are never downgraded; tablets behind it install this one."
        ) if older else ""

    warnings = []
    if older_text:
        warnings.append({"code": "older", "message": older_text})
    if moving and release.update_type == UpdateType.MANDATORY:
        warnings.append({
            "code": "mandatory",
            "message": f"{label} is Mandatory: tablets at {'this branch' if len(moving) == 1 else f'these {len(moving)} branches'} "
                       "cannot log in until they install it.",
        })
    if release.update_type == UpdateType.ANYTIME:
        unset = [r["branch"].name for r in moving if not r["has_working_time"]]
        if unset:
            warnings.append({
                "code": "working_time",
                "message": f"{_branches(len(unset), 'has', 'have')} no working time, "
                           f"so an Any time build is not offered there until it is set: "
                           f"{', '.join(unset[:5])}{'…' if len(unset) > 5 else ''}.",
            })
    return {
        "summary": summary,
        "moving": len(moving),
        "warnings": warnings,
    }


def map_release(releases_qs, release_pk, *, user, scope, branches, targets):
    """Point branches (or All) at a release. Each target's active row is switched
    off -- it stays as history -- and a new row inserted. Returns the number of
    rows written. `branches`/`targets` come from the caller's company scope."""
    if scope not in ReleaseScope.values:
        raise DeviceActionError("Choose All branches or selected branches.", "Target")
    if scope == ReleaseScope.BRANCH and not targets:
        raise DeviceActionError("Tick at least one branch.", "Branches")
    try:
        with transaction.atomic():
            release = _locked_release(releases_qs, release_pk)
            if not release.is_active:
                raise DeviceActionError(f"{release} is withdrawn. Restore it before mapping it.", "Release")
            same_app = AppReleaseMapping.objects.select_for_update().filter(
                company_id=release.company_id, channel=release.channel, is_active=True,
            )
            if scope == ReleaseScope.ALL:
                current = same_app.filter(scope=ReleaseScope.ALL).first()
                if current is not None and current.release_id == release.pk:
                    raise DeviceActionError(f"{release} is already the All-branches release.", "Release")
                if current is not None:
                    _switch_off(current, user)
                AppReleaseMapping.objects.create(
                    release=release, company_id=release.company_id, channel=release.channel,
                    scope=ReleaseScope.ALL, created_by=user,
                )
                return 1

            for branch in targets:
                if branch.company_id != release.company_id or not branch.is_active:
                    raise DeviceActionError("Choose active branches of this company.", "Branches")
            current = {m.branch_id: m for m in same_app.filter(branch__in=targets)}
            written = 0
            for branch in targets:
                existing = current.get(branch.pk)
                if existing is not None and existing.release_id == release.pk:
                    continue
                if existing is not None:
                    _switch_off(existing, user)
                AppReleaseMapping.objects.create(
                    release=release, company_id=release.company_id, channel=release.channel,
                    scope=ReleaseScope.BRANCH, branch=branch, created_by=user,
                )
                written += 1
            if not written:
                raise DeviceActionError(f"Every ticked branch is already on {release}.", "Branches")
            return written
    except IntegrityError:
        # Two admins mapping the same branch at once: the unique index let one
        # through. Nothing of this attempt was saved.
        raise DeviceActionError("Someone changed this mapping just now. Reload and try again.")


def _switch_off(mapping, user):
    mapping.is_active = False
    mapping.modified_by = user
    mapping.save(update_fields=["is_active", "modified_by", "modified_on"])


def unmap_release(mappings_qs, pk, *, user):
    """Switch a mapping off. A branch falls back to the All row; switching the
    All row off leaves branches without their own mapping with no update."""
    with transaction.atomic():
        mapping = mappings_qs.select_for_update().filter(pk=pk).first()
        if mapping is None:
            raise DeviceActionError("That mapping was not found.")
        if not mapping.is_active:
            raise DeviceActionError("That mapping is already removed.")
        _switch_off(mapping, user)
    return mapping


# -- Device Settings: how a station prints and prices (section 8.7) ------------
#
# One active row per station. The tablet downloads it after login and keeps it,
# logo and all, so a station can trade and print while it is offline -- which is
# why the bitmap travels in the payload rather than behind a link.

SETTINGS_TEXT = {
    "header_1": ("Header 1", 48, True),
    "header_2": ("Header 2", 48, True),
    "additional_header_1": ("Additional Header 1", 75, False),
    "additional_header_2": ("Additional Header 2", 75, False),
    "footer_1": ("Footer 1", 75, True),
    "footer_2_arabic": ("Footer 2 (Arabic)", 48, False),
}

# What "Apply to other stations" may copy: the wording and the printer traits a
# network shares. Never the station, its code, its prefix, its own headers or
# its logo -- those are what make one station's receipt its own. The client's
# live rows show why it is needed: one legal name in four spellings, one footer
# in two, after years of editing 95 rows by hand.
SETTINGS_SHAREABLE = {
    "additional_header_1": "Additional Header 1",
    "additional_header_2": "Additional Header 2",
    "footer_1": "Footer 1",
    "footer_2_arabic": "Footer 2 (Arabic)",
    "paper_feed": "Paper Feed",
    "print_type": "Print Type",
    "print_logo": "Print Logo",
    "receipt_copies": "No of Receipt Copy",
    "share_on_whatsapp": "Share on WhatsApp",
    "round_off_mode": "Round Off Type",
    "round_off_step": "Round Off Limit",
    "customer_test_minutes": "Customer Test Time Slot",
    "cashier_test_minutes": "Cashier Test Time Slot",
}

SETTINGS_CODE_MAX = 15
PREFIX_MAX = 5
LOGO_MAX_BYTES = 1024 * 1024
# The print head's width in dots on the 80 mm printers in use: every live logo
# is a 1-bit bitmap 832 dots wide.
PRINTER_DOTS = 832


def _text(value):
    return str(value or "").strip()


def _whole(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None


def _check_settings(data, *, branch, instance, settings_qs, branches_qs):
    """(fields, errors) for a posted settings form.

    Every rule the model's constraints hold, said as a sentence first: a
    constraint that fires reaches the admin as a 500, not as help.
    """
    errors = []
    fields = {}
    # The company these codes must be unique within -- the branch being
    # settled (new row) or the row's own branch (edit). `settings_qs` is the
    # caller's scope, which for a system user spans every company, so it is
    # not narrow enough on its own to say "unique per company".
    company_id = branch.company_id if branch is not None else (
        instance.company_id if instance is not None else None
    )

    if instance is None:
        if branch is None:
            errors.append({"field": "Station", "message": "Choose a station."})
        elif branches_qs.filter(pk=branch.pk, is_active=True).first() is None:
            errors.append({"field": "Station", "message": "Choose an active station of this company."})
        elif settings_qs.filter(branch=branch, is_active=True).exists():
            errors.append({
                "field": "Station",
                "message": f"{branch.name} already has settings. Edit them, or deactivate them first.",
            })

    code = _text(data.get("settings_code"))
    if not code:
        errors.append({"field": "Settings Code", "message": "Enter a settings code."})
    elif len(code) > SETTINGS_CODE_MAX:
        errors.append({"field": "Settings Code",
                       "message": f"Keep the code to {SETTINGS_CODE_MAX} characters."})
    elif company_id is not None:
        clash = settings_qs.filter(is_active=True, company_id=company_id, settings_code__iexact=code)
        if instance is not None:
            clash = clash.exclude(pk=instance.pk)
        if clash.exists():
            errors.append({"field": "Settings Code", "message": f"{code} is already used by another station."})
    fields["settings_code"] = code

    prefix = _text(data.get("order_no_prefix")).upper()
    if not prefix:
        errors.append({"field": "Order No Starting Characters", "message": "Enter the order number prefix."})
    elif len(prefix) > PREFIX_MAX or not prefix.isalnum():
        errors.append({
            "field": "Order No Starting Characters",
            "message": f"Up to {PREFIX_MAX} letters or digits, like DUBPP.",
        })
    elif company_id is not None:
        clash = settings_qs.filter(is_active=True, company_id=company_id, order_no_prefix=prefix)
        if instance is not None:
            clash = clash.exclude(pk=instance.pk)
        holder = clash.select_related("branch").first()
        if holder is not None:
            errors.append({
                "field": "Order No Starting Characters",
                "message": f"{holder.branch.name} already prints with {prefix}. "
                           "Two stations sharing a prefix could print the same receipt number.",
            })
    fields["order_no_prefix"] = prefix

    for name, (label, limit, required) in SETTINGS_TEXT.items():
        value = _text(data.get(name))
        if required and not value:
            errors.append({"field": label, "message": f"Enter {label.lower()}."})
        elif len(value) > limit:
            errors.append({"field": label, "message": f"Keep {label.lower()} to {limit} characters."})
        fields[name] = value

    feed = _whole(data.get("paper_feed"))
    if feed is None or not 0 <= feed <= 10:
        errors.append({"field": "Paper Feed", "message": "Blank lines after the receipt, 0 to 10."})
    fields["paper_feed"] = feed

    copies = _whole(data.get("receipt_copies"))
    if copies is None or not 1 <= copies <= 10:
        errors.append({"field": "No of Receipt Copy", "message": "Between 1 and 10 copies."})
    fields["receipt_copies"] = copies

    for name, label in (("customer_test_minutes", "Customer Test Time Slot"),
                        ("cashier_test_minutes", "Cashier Test Time Slot")):
        minutes = _whole(data.get(name))
        if minutes is None or not 0 < minutes <= 240:
            errors.append({"field": label, "message": "Minutes, between 1 and 240."})
        fields[name] = minutes

    print_type = _text(data.get("print_type")) or PrintType.LANDSCAPE
    if print_type not in PrintType.values:
        errors.append({"field": "Print Type", "message": "Choose Portrait or Landscape."})
    fields["print_type"] = print_type

    mode = _text(data.get("round_off_mode")) or RoundOffMode.UPWARD
    if mode not in RoundOffMode.values:
        errors.append({"field": "Round Off Type", "message": "Choose Nearest, Upward or Downward."})
    fields["round_off_mode"] = mode

    steps = [value for value, _ in ROUND_OFF_STEPS]
    try:
        step = Decimal(str(data.get("round_off_step")))
    except (InvalidOperation, TypeError, ValueError):
        step = None
    if step not in steps:
        errors.append({"field": "Round Off Limit", "message": "Choose 25 fils, 50 fils or 1 AED."})
    fields["round_off_step"] = step

    fields["print_logo"] = bool(data.get("print_logo"))
    fields["share_on_whatsapp"] = bool(data.get("share_on_whatsapp"))
    return fields, errors


def save_settings(settings_qs, branches_qs, *, user, pk=None, data):
    """Add or edit one station's settings. The station is fixed once saved: the
    row is what that station prints, and moving it would rewrite two stations'
    receipts at once."""
    with transaction.atomic():
        instance = None
        if pk:
            instance = settings_qs.select_for_update().filter(pk=pk).first()
            if instance is None:
                raise DeviceActionError("Those settings were not found.")
        branch = None
        if instance is None:
            branch = branches_qs.filter(pk=_whole(data.get("branch"))).first()

        fields, errors = _check_settings(
            data, branch=branch, instance=instance,
            settings_qs=settings_qs, branches_qs=branches_qs,
        )
        if errors:
            raise Invalid(errors)

        if instance is None:
            row = DeviceSettings(branch=branch, created_by=user, **fields)
            row.apply_approval_defaults()
        else:
            row = instance
            for name, value in fields.items():
                setattr(row, name, value)
            row.modified_by = user
        row.save()
        return row


def _locked_settings(settings_qs, pk):
    row = settings_qs.select_for_update().filter(pk=pk).first()
    if row is None:
        raise DeviceActionError("Those settings were not found.")
    return row


def bitmap_warnings(raw, name=""):
    """What is odd about an uploaded logo, said but not refused.

    The printers take a 1-bit bitmap as wide as the head; every live logo is
    one. But the client's own data has a 32-bit 100x100 BMP in it, so a station
    that wants to print something unusual is not stopped -- it is told.
    """
    notes = []
    if raw[:2] != b"BM":
        notes.append("This is not a BMP. The printers take a bitmap; anything else may not print.")
        return notes
    try:
        width = int.from_bytes(raw[18:22], "little", signed=True)
        bits = int.from_bytes(raw[28:30], "little")
    except (IndexError, ValueError):
        return ["This BMP could not be read past its header."]
    if bits != 1:
        notes.append(f"This is a {bits}-bit bitmap. The printers take a 1-bit (black and white) one.")
    if width > PRINTER_DOTS:
        notes.append(f"It is {width} dots wide; the print head is {PRINTER_DOTS}. It will be cut off.")
    return notes


def set_logo(settings_qs, pk, *, user, raw, name="", now=None):
    """Store the bitmap exactly as it arrived.

    Never re-encoded: the bytes are what the print head receives, so a resize or
    a colour conversion would change what a station prints. Stamping
    logo_changed_on is what tells a tablet to take the new one
    (Service_Get_Device_SEttings.sql:46-50).
    """
    if not raw:
        raise DeviceActionError("Choose a file to upload.", "Logo")
    if len(raw) > LOGO_MAX_BYTES:
        raise DeviceActionError(
            f"That file is {len(raw) // 1024} KB. Keep the logo under {LOGO_MAX_BYTES // 1024} KB.", "Logo",
        )
    with transaction.atomic():
        row = _locked_settings(settings_qs, pk)
        row.logo = raw
        row.logo_name = (name or "")[:120]
        row.logo_changed_on = now or timezone.now()
        row.modified_by = user
        row.save(update_fields=["logo", "logo_name", "logo_changed_on", "modified_by", "modified_on"])
    return row, bitmap_warnings(raw, name)


def clear_logo(settings_qs, pk, *, user, now=None):
    with transaction.atomic():
        row = _locked_settings(settings_qs, pk)
        if not row.logo:
            raise DeviceActionError("There is no logo to remove.", "Logo")
        row.logo = None
        row.logo_name = ""
        # Still a change: the tablet has to drop the one it holds.
        row.logo_changed_on = now or timezone.now()
        row.modified_by = user
        row.save(update_fields=["logo", "logo_name", "logo_changed_on", "modified_by", "modified_on"])
    return row


def apply_settings_to(settings_qs, pk, *, user, fields, branches):
    """Copy the ticked fields from one station's settings onto others.

    Returns (written, skipped): a station with no active settings of its own is
    skipped rather than invented -- its code and prefix are nobody's to guess.
    """
    wanted = [f for f in fields if f in SETTINGS_SHAREABLE]
    if not wanted:
        raise DeviceActionError("Tick at least one field to copy.", "Fields")
    if not branches:
        raise DeviceActionError("Tick at least one station.", "Stations")

    with transaction.atomic():
        source = _locked_settings(settings_qs, pk)
        values = {name: getattr(source, name) for name in wanted}
        # Ticking the station it came from is harmless, and it is not a station
        # that was "skipped for having no settings".
        elsewhere = [b for b in branches if b.pk != source.branch_id]
        targets = (
            settings_qs.select_for_update()
            .filter(branch__in=elsewhere, is_active=True)
            .exclude(pk=source.pk)
        )
        written = 0
        for row in targets:
            for name, value in values.items():
                setattr(row, name, value)
            row.modified_by = user
            row.save(update_fields=[*values, "modified_by", "modified_on"])
            written += 1
        return written, len(elsewhere) - written


def deactivate_settings(settings_qs, pk, *, user):
    """Switch a station's settings off. The row stays: bill_continuity took a
    copy of its prefix, and receipts printed under it are still out there."""
    with transaction.atomic():
        row = _locked_settings(settings_qs, pk)
        if not row.is_active:
            raise DeviceActionError("Those settings are already inactive.")
        row.is_active = False
        row.modified_by = user
        row.save(update_fields=["is_active", "modified_by", "modified_on"])
    return row


def settings_payload(branch, *, since=None):
    """What a tablet at `branch` is told after login, or None if nobody has set
    it up yet.

    The logo travels inside this payload, base64, exactly as the legacy sent it:
    a station prints all day without a network, so a link it might not be able
    to follow is no use. It is left out when the tablet's copy is already
    current -- `since` is the moment it last synced -- which is the legacy's own
    rule (Service_Get_Device_SEttings.sql:46-50).
    """
    row = (
        DeviceSettings.objects.with_logo()
        .filter(branch=branch, is_active=True, approval_status=ApprovalStatus.APPROVED)
        .select_related("branch__company")
        .first()
    )
    if row is None:
        return None

    company = row.branch.company
    fresh = since is None or row.logo_changed_on is None or row.logo_changed_on >= since
    logo = bytes(row.logo) if row.logo else b""
    return {
        "settings_code": row.settings_code,
        "station": row.branch.name,
        "header_1": row.header_1,
        "header_2": row.header_2,
        "additional_header_1": row.additional_header_1,
        "additional_header_2": row.additional_header_2,
        "footer_1": row.footer_1,
        "footer_2_arabic": row.footer_2_arabic,
        "paper_feed": row.paper_feed,
        "print_type": row.print_type,
        "print_logo": row.print_logo,
        "receipt_copies": row.receipt_copies,
        "share_on_whatsapp": row.share_on_whatsapp,
        "order_no_prefix": row.order_no_prefix,
        "customer_test_minutes": row.customer_test_minutes,
        "cashier_test_minutes": row.cashier_test_minutes,
        "round_off_mode": row.round_off_mode,
        "round_off_step": str(row.round_off_step),
        "tax_type": company.tax_type,
        "discount_type": company.discount_type,
        "logo_changed_at": row.logo_changed_on,
        # Base64 of the bitmap itself, or "" when the tablet's copy is current.
        "logo": base64.b64encode(logo).decode() if (fresh and logo) else "",
    }


# -- Receipt numbering -------------------------------------------------------


def bill_prefix_for(branch):
    """The order prefix a device takes a copy of when it first counts at `branch`.

    The station's approved, active settings prefix; otherwise its branch code --
    the same fallback the legacy used (Service_Operator_Login.sql:747-755).
    Upper-cased, as the legacy did when it built each number.
    """
    settings = (
        DeviceSettings.objects
        .filter(branch=branch, is_active=True, approval_status=ApprovalStatus.APPROVED)
        .values_list("order_no_prefix", flat=True)
        .first()
    )
    return (settings or branch.short_code or "").upper()


def counter_for(device, branch, kind):
    """This device's BillContinuity row at `branch`, for `kind` -- created at
    last_number=0, seeded with the branch's prefix, the first time this
    device counts here.

    Ported from the inline IF EXISTS ... ELSE INSERT block in
    Service_Operator_Login.sql -- the legacy did this check-and-create right
    inside the login proc, once per bill kind. Called twice at login (order,
    test_ride); every later count-up is the tablet's own job, offline.
    """
    counter, _ = BillContinuity.objects.get_or_create(
        device=device, branch=branch, kind=kind,
        defaults={"prefix": bill_prefix_for(branch)},
    )
    return counter


def format_bill_number(counter, number=None):
    """The printed receipt number for `number` (default: the counter's next).

    [T] + prefix + device registration id + number zero-padded to six digits,
    with no separator -- exactly the legacy's shape, so receipts look the same
    to cashiers and customers: DUBPP + 60182 + 000335 = DUBPP60182000335, and a
    test ride TDUBPP60182000006.

    The missing separator is ambiguous to read back (ABCO2+60179 against
    ABCO+260179), but nothing needs to: orders store the device and the number
    as separate columns. Past 999999 the number simply grows a digit.
    """
    n = counter.next_number if number is None else number
    lead = "T" if counter.kind == BillKind.TEST_RIDE else ""
    return f"{lead}{counter.prefix}{counter.device.device_registration_id}{n:0{BILL_NUMBER_WIDTH}d}"

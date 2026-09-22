"""Device Management screens.

Markup ported from the wireframe's byky_device app; every value comes from our
own models. Nothing is seeded or fabricated, so an empty database renders empty
tabs and zero tiles.

Four screens:

  Device Approval  the onboarding queue. A tablet registers itself, an admin
                   names it and gives it a station, and only then can anyone
                   log in on it.
  Device Mapping   which approved device is at which station, since when, and
                   who is on it now.
  App Releases     the catalogue of app builds (update-check-api.md).
  App Mapping      which build each branch is offered, and the All-branches
                   default.
  Device Settings  how each station's tablets print and price, logo included.

Every action writes through services.py, where the rules live.
"""

from django.db.models import F, Func, IntegerField, Value
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponse, JsonResponse
from django.urls import reverse
from django.views import View

from apps.company.scoping import branches_for, companies_for
from apps.company import writes
from apps.devices import drawers, scoping, services
from apps.company.models import Company
from apps.devices.models import (
    ROUND_OFF_STEPS,
    AppRelease,
    AppReleaseMapping,
    Device,
    DeviceMapping,
    DeviceSettings,
    DeviceStatus,
    PrintType,
    ReleaseScope,
    RoundOffMode,
    UpdateType,
)
from apps.portal.permissions import PagePermissionMixin
from apps.portal.screens import PrivilegeScreenView
from apps.portal.services import has_permission
from theme import drawers as theme_drawers
from theme.views import ThemedTemplateView

ACTIONS = ("create", "read", "update", "delete", "approve", "print")


class DeviceScreenView(PagePermissionMixin, ThemedTemplateView):
    """Shared by every screen in this module: the reference lists its drawers
    resolve against, and the permission flags its buttons check."""

    drawer_specs = drawers.SPECS

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context.update({
            "branches_list": list(
                branches_for(user).filter(is_active=True).values("id", "name")
            ),
            "perm": {
                action: has_permission(user, self.page_code, action)
                for action in ACTIONS
            },
        })
        return context

    def render_to_response(self, context, **response_kwargs):
        # Resolved here, not in get_context_data, so a subclass's own lists are
        # already in context when a drawer's dropdowns are filled.
        specs = theme_drawers.all_for_user(
            self.drawer_specs, self.request.user.sees_every_company
        )
        context.update(theme_drawers.resolve_all(specs, context))
        return super().render_to_response(context, **response_kwargs)


def _latest_sessions(user, device_ids):
    """The most recent session per device, and which devices are in use now.

    One query for each, rather than one per row. `distinct("device")` is
    Postgres's DISTINCT ON, which is why the ordering starts with device_id.
    """
    if not device_ids:
        return {}, set()
    sessions = (
        scoping.device_sessions_for(user)
        .filter(device_id__in=device_ids)
        .select_related("user")
        .order_by("device_id", "-logged_in_at")
        .distinct("device_id")
    )
    latest = {s.device_id: s for s in sessions}
    open_now = set(
        scoping.device_sessions_for(user)
        .filter(device_id__in=device_ids, logged_out_at__isnull=True)
        .values_list("device_id", flat=True)
    )
    return latest, open_now


def _device_row(device):
    """The identity columns both screens share."""
    return {
        "pk": device.pk,
        "registration_id": device.device_registration_id,
        "name": device.name,
        "model": device.device_model,
        "platform": device.get_platform_display(),
        "channel": device.get_channel_display(),
        "channel_value": device.channel,
        "status": device.status,
        "status_label": device.get_status_display(),
    }


def _action_context(user):
    """What the action modals need, shared by the list and the detail page:
    the stations a device can be placed at, and the five action URLs.

    No "replaces an existing device" list: picking from every device of the
    company by hand was not workable. The replacement service remains
    (section 9B.4) for when a better way to choose is designed."""
    return {
        "approve_stations": list(
            branches_for(user).filter(is_active=True).order_by("name").values("id", "name")
        ),
        "action_urls": {
            action: reverse(f"devices-device-{action}", args=[0])
            for action in ("approve", "reconnect", "reject", "block", "unblock")
        },
    }


class DeviceApprovalView(DeviceScreenView):
    """The onboarding queue: Pending, Approved, Blocked.

    A read-only monitor with actions. There is no Add button -- a device joins
    this list by registering itself, and an admin who could create one by hand
    could invent a station's identity.
    """

    template_name = "devices/device_approval_list.html"
    page_code = "devices.device_approval"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        devices = list(
            scoping.devices_for(self.request.user)
            .select_related("reconnect_of", "approved_by")
            .order_by("-created_on")
        )
        # The station comes from the open mapping row -- one query for all of
        # them, rather than one per device through `current_branch`.
        open_mappings = {
            m.device_id: m.branch
            for m in scoping.mappings_for(self.request.user)
            .filter(device__in=devices, to_date__isnull=True)
            .select_related("branch")
        }
        stations = {pk: branch.name for pk, branch in open_mappings.items()}

        queues = {"pending": [], "approved": [], "blocked": []}
        for device in devices:
            if device.status == DeviceStatus.RETIRED:
                continue
            row = _device_row(device)
            row.update({
                "registered_on": device.created_on,
                "last_seen_at": device.last_seen_at,
                "branch": stations.get(device.pk, ""),
                "detail_url": reverse(
                    "devices-device-approval-detail",
                    args=[device.device_registration_id],
                ),
                # A reinstall the server recognised: shown so the admin can see
                # which tablet this claims to be before deciding.
                "reconnect_of": (
                    {
                        "name": device.reconnect_of.name,
                        "registration_id": device.reconnect_of.device_registration_id,
                        "last_seen_at": device.reconnect_of.last_seen_at,
                    }
                    if device.reconnect_of_id
                    else None
                ),
            })
            queues.setdefault(device.status, []).append(row)

        context.update(_action_context(self.request.user))
        context.update({
            "pending_devices": queues["pending"],
            "approved_devices": queues["approved"],
            "blocked_devices": queues["blocked"],
            "queue_counts": {
                "pending": len(queues["pending"]),
                "approved": len(queues["approved"]),
                "blocked": len(queues["blocked"]),
            },
        })
        return context


class DeviceApprovalDetailView(DeviceScreenView):
    """One registration, in full, with its status history.

    Keyed on the registration number rather than the primary key: that number is
    the one thing an admin is told over the phone.
    """

    template_name = "devices/device_approval_detail.html"
    page_code = "devices.device_approval"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        registration_id = self.kwargs.get("registration_id")
        device = (
            scoping.devices_for(self.request.user)
            .select_related("approved_by", "reconnect_of", "replaced_device")
            .filter(device_registration_id=registration_id)
            .first()
        )
        context.update(_action_context(self.request.user))
        context.update({
            "registration_id": registration_id,
            "device": device,
            "status_log": (
                scoping.status_log_for(self.request.user)
                .filter(device=device)
                .select_related("created_by", "related_device")
                if device
                else []
            ),
            "mapping_history": (
                scoping.mappings_for(self.request.user)
                .filter(device=device)
                .select_related("branch")
                if device
                else []
            ),
        })
        return context


class DeviceMappingView(DeviceScreenView):
    """Approved devices and the station each is mapped to.

    One row per device, showing its open mapping -- the legacy grid showed only
    the current one too. The full history is on the device's detail page.
    """

    template_name = "devices/device_mapping_list.html"
    page_code = "devices.device_mapping"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        devices = list(
            scoping.devices_for(user)
            .filter(status__in=[DeviceStatus.APPROVED, DeviceStatus.BLOCKED])
            .order_by("name", "device_registration_id")
        )
        device_ids = [d.pk for d in devices]

        open_mappings = {
            m.device_id: m
            for m in scoping.mappings_for(user)
            .filter(device_id__in=device_ids, to_date__isnull=True)
            .select_related("branch")
        }
        latest_sessions, in_use = _latest_sessions(user, device_ids)

        rows = []
        for device in devices:
            mapping = open_mappings.get(device.pk)
            session = latest_sessions.get(device.pk)
            online = device.pk in in_use
            blocked = device.status == DeviceStatus.BLOCKED
            row = _device_row(device)
            row.update({
                "station": mapping.branch.name if mapping else "",
                "station_id": mapping.branch_id if mapping else "",
                "from_date": mapping.from_date if mapping else None,
                "app_version": session.app_version if session else "",
                "last_login": session.logged_in_at if session else None,
                "employee": (
                    session.user.display_name or session.user.username
                    if session
                    else ""
                ),
                "online": online,
                "blocked": blocked,
                "mapped": mapping is not None,
                # Where the closed mappings live: a row here is the open one, so
                # its to_date is always empty. The history is on the device.
                "detail_url": reverse(
                    "devices-device-approval-detail",
                    args=[device.device_registration_id],
                ),
            })
            rows.append(row)

        # Devices with no station -- the pool the Add drawer offers. The legacy
        # proc excluded open mappings the same way. Blocked ones are included:
        # an operator device must be mapped before it can be unblocked.
        mapped_ids = set(open_mappings)
        unmapped = [
            {
                "id": d.pk,
                "name": (
                    f"{d.device_registration_id} — {d.name or d.device_model or 'Unnamed device'}"
                    + (" (blocked)" if d.status == DeviceStatus.BLOCKED else "")
                ),
            }
            for d in devices
            if d.pk not in mapped_ids
        ]

        context.update({
            "devices": rows,
            "unmapped_devices_list": unmapped,
            "remap_stations": list(
                branches_for(user).filter(is_active=True).order_by("name").values("id", "name")
            ),
            "mapping_urls": {
                "save": reverse("devices-mapping-save"),
                "remap": reverse("devices-mapping-remap", args=[0]),
                "close": reverse("devices-mapping-close", args=[0]),
            },
            "counts": {
                "total": len(rows),
                "online": sum(1 for r in rows if r["online"]),
                "offline": sum(1 for r in rows if not r["online"] and not r["blocked"]),
                "stations": len({r["station"] for r in rows if r["station"]}),
                "blocked": sum(1 for r in rows if r["blocked"]),
            },
            # Which stations already hold a device, so the drawer can warn
            # before the server refuses (Branch.is_multi_device).
            "branch_flags": [
                {
                    "id": b.pk,
                    "name": b.name,
                    "multi_device": b.is_multi_device,
                    "open_count": DeviceMapping.objects.filter(
                        branch=b, to_date__isnull=True
                    ).count(),
                }
                for b in branches_for(user).filter(is_active=True)
            ],
        })
        return context


class DevicePrivilegeView(PrivilegeScreenView):
    """Device Management's copy of the shared privilege grid."""

    page_code = "devices.privileges"
    module_code = "devices"
    module_label = "Device Management"


# --- Device Approval actions ---------------------------------------------------


class DeviceActionView(writes.WriteView):
    """POST one admin action on one device; JSON {ok, message | errors}.

    The permission is checked here and again by nothing else, so every action
    names its own. The device is looked up through the admin's company scope
    inside the service, under a row lock.
    """

    model = Device
    page_code = "devices.device_approval"
    permission = "approve"
    done = "Done."

    def act(self, devices, pk, data):
        raise NotImplementedError

    def post(self, request, pk, *args, **kwargs):
        if not self.may(self.permission):
            return self.refused()
        data = writes._payload(request)
        try:
            device = self.act(scoping.devices_for(request.user), pk, data)
        except services.DeviceActionError as refused:
            return JsonResponse({
                "ok": False, "code": "invalid",
                "errors": [{"field": refused.field, "message": refused.message}],
            }, status=400)
        return JsonResponse({"ok": True, "message": self.done.format(device=device)})


def _reason(data):
    return str(data.get("reason") or "").strip()


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class DeviceApproveView(DeviceActionView):
    done = "{device} approved."

    def act(self, devices, pk, data):
        branch = None
        branch_id = _int(data.get("branch"))
        if branch_id is not None:
            branch = branches_for(self.request.user).filter(pk=branch_id).first()
            if branch is None:
                raise services.DeviceActionError("Choose an active station of this company.", "Station")
        return services.approve_device(
            devices, pk, user=self.request.user, name=data.get("name"),
            branch=branch, replaces_pk=_int(data.get("replaces")),
        )


class DeviceReconnectView(DeviceActionView):
    done = "{device} reconnected. It keeps its number."

    def act(self, devices, pk, data):
        return services.reconnect_device(devices, pk, user=self.request.user)


class DeviceRejectView(DeviceActionView):
    done = "Registration rejected."

    def act(self, devices, pk, data):
        return services.reject_device(devices, pk, user=self.request.user, reason=_reason(data))


class DeviceBlockView(DeviceActionView):
    permission = "update"
    done = "{device} blocked. Its sessions were closed."

    def act(self, devices, pk, data):
        return services.block_device(devices, pk, user=self.request.user, reason=_reason(data))


class DeviceUnblockView(DeviceActionView):
    permission = "update"
    done = "{device} unblocked."

    def act(self, devices, pk, data):
        return services.unblock_device(devices, pk, user=self.request.user, reason=_reason(data))


# --- Device Mapping writes ------------------------------------------------------


class MappingActionView(DeviceActionView):
    page_code = "devices.device_mapping"

    def branch(self, data):
        branch = branches_for(self.request.user).filter(pk=_int(data.get("branch"))).first()
        if branch is None:
            raise services.DeviceActionError("Choose a station.", "Station")
        return branch


class DeviceMapSaveView(MappingActionView):
    """The Add drawer: a device with no station gets one. The device comes in
    the body, so there is no pk in the URL."""

    permission = "create"
    done = "Device mapped."

    def post(self, request, *args, **kwargs):
        data = writes._payload(request)
        return super().post(request, _int(data.get("device")) or 0)

    def act(self, devices, pk, data):
        return services.map_device(devices, pk, user=self.request.user, branch=self.branch(data))


class DeviceRemapView(MappingActionView):
    permission = "update"
    done = "Device moved to its new station from now."

    def act(self, devices, pk, data):
        return services.remap_device(devices, pk, user=self.request.user, branch=self.branch(data))


class DeviceCloseMappingView(MappingActionView):
    permission = "delete"
    done = "Mapping closed now. The device has no station until it is mapped again."

    def act(self, devices, pk, data):
        return services.close_mapping(devices, pk, user=self.request.user)


# --- App Releases and App Mapping ----------------------------------------------
#
# The catalogue of builds, and which branches get which. What a tablet is then
# told is services.release_for / update_decision; the rows written here are all
# it reads. Contract: design/registration/update-check-api.md.


def _companies(user):
    """Every active company this user manages, in name order.

    One server, several companies: a company user has exactly one, a system user
    (BYKY's own staff, who belong to none) has all of them. The screens below
    are built for both -- a Company column and a filter appear only when there
    is more than one, so a single-company deployment looks unchanged.
    """
    return list(companies_for(user).filter(is_active=True).order_by("name"))


def _posted_company(user, posted):
    """The company a new row belongs to.

    A company user never chooses: it is theirs, and a posted value is ignored,
    so a crafted request writes into their own company or nowhere. A system user
    belongs to none, so they must choose, and the choice is checked against what
    they may see.
    """
    companies = _companies(user)
    if not user.sees_every_company:
        if not companies:
            raise services.DeviceActionError("Your account has no company.", "Company")
        return companies[0].pk
    chosen = _int(posted)
    if chosen is None or chosen not in {c.pk for c in companies}:
        raise services.DeviceActionError("Choose the company this belongs to.", "Company")
    return chosen


def _app_choices():
    return [{"id": c.value, "name": c.label} for c in services.APP_CHANNELS]


class AppReleaseView(DeviceScreenView):
    """Every build of every app. Adding one reaches no tablet until it is
    mapped on App Mapping."""

    template_name = "devices/app_release_list.html"
    page_code = "devices.app_release"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        releases = list(
            scoping.releases_for(user)
            .select_related("created_by", "company")
            .order_by("channel", "-version_code")
        )
        # Where each build is live, in one query.
        usage = {}
        for release_id, scope in (
            scoping.release_mappings_for(user)
            .filter(is_active=True)
            .values_list("release_id", "scope")
        ):
            entry = usage.setdefault(release_id, {"branches": 0, "all": False})
            if scope == ReleaseScope.ALL:
                entry["all"] = True
            else:
                entry["branches"] += 1

        rows = []
        for r in releases:
            used = usage.get(r.pk, {"branches": 0, "all": False})
            rows.append({
                "pk": r.pk,
                "app": r.get_channel_display(),
                "channel": r.channel,
                "company": r.company.name,
                "company_code": r.company.short_code,
                "version_name": r.version_name,
                "version_code": r.version_code,
                "update_type": r.update_type,
                "update_type_label": r.get_update_type_display(),
                "mandatory": r.is_mandatory,
                "note": r.update_note,
                "url": r.download_url,
                "branch_count": used["branches"],
                "is_all": used["all"],
                "mapped": used["all"] or used["branches"] > 0,
                "active": r.is_active,
                "added_by": services.person_name(r.created_by),
                "added_on": r.created_on,
                "withdraw_note": services.withdraw_consequence(r) if r.is_active else "",
                "json_id": f"release-{r.pk}",
                "fields_json": {
                    "pk": r.pk,
                    "title": f"{r.get_channel_display()} {r.version_name}",
                    "channel": r.channel,
                    "company": str(r.company_id),
                    "version_name": r.version_name,
                    "version_code": str(r.version_code),
                    "update_type": r.update_type,
                    "download_url": r.download_url,
                    "update_note": r.update_note,
                    "mapped_count": used["branches"] + (1 if used["all"] else 0),
                    "is_all": used["all"],
                    "branch_count": used["branches"],
                },
            })

        companies = _companies(user)
        context.update({
            "releases": rows,
            "companies": companies,
            "multi_company": len(companies) > 1,
            "release_apps": _app_choices(),
            "release_update_types": [{"id": v, "name": label} for v, label in UpdateType.choices],
            # A system user manages several companies, so a release says which
            # one it is for; a company user has only theirs and never picks.
            "release_companies": [{"id": c.pk, "name": c.name} for c in companies],
            "release_company_note": (
                f"Company: {companies[0].name}" if len(companies) == 1 else
                "A release belongs to one company. Version codes rise per company and app."
            ),
            "release_urls": {
                "save": reverse("devices-release-save"),
                "withdraw": reverse("devices-release-withdraw", args=[0]),
                "restore": reverse("devices-release-restore", args=[0]),
            },
            "release_counts": {
                "total": len(rows),
                "withdrawn": sum(1 for r in rows if not r["active"]),
                "mandatory": sum(1 for r in rows if r["active"] and r["mandatory"]),
                "mapped": sum(1 for r in rows if r["active"] and r["mapped"]),
            },
        })
        return context


class AppMappingView(DeviceScreenView):
    """Which build each branch is offered, per app, and the All-branches
    default the rest fall back to."""

    template_name = "devices/app_mapping_list.html"
    page_code = "devices.app_release_mapping"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        companies = _companies(user)
        branches = list(
            branches_for(user).filter(is_active=True, company__in=companies)
            .select_related("location", "company").order_by("company__name", "name")
        )
        releases = list(
            scoping.releases_for(user).filter(company__in=companies, is_active=True)
            .order_by("-version_code")
        )

        # Mapping is per company *and* per app: the All-branches default and the
        # one-active-row-per-branch index are both scoped that way. So each app
        # tab holds one block per company -- one block, and it reads as before.
        apps = []
        for channel in services.APP_CHANNELS:
            blocks = []
            for company in companies:
                own_branches = [b for b in branches if b.company_id == company.pk]
                all_row, rows = services.branch_release_state(company.pk, channel, own_branches)
                following = sum(1 for r in rows if r["source"] != "own")
                blocks.append({
                    "company": company,
                    "all": all_row,
                    "all_by": services.person_name(all_row.created_by) if all_row else "",
                    "all_remove_note": (
                        f"{following} branch{'es' if following != 1 else ''} of {company.name} "
                        "without their own mapping will be offered no update until a new default "
                        "is set. Branches with their own mapping are not affected."
                    ),
                    "rows": rows,
                    "own_count": sum(1 for r in rows if r["source"] == "own"),
                    "none_count": sum(1 for r in rows if r["source"] == "none"),
                })
            apps.append({
                "value": channel.value,
                "label": channel.label,
                "blocks": blocks,
                "own_count": sum(b["own_count"] for b in blocks),
                "rows_count": sum(len(b["rows"]) for b in blocks),
                "choices": {
                    str(company.pk): [
                        {"id": r.pk,
                         "label": f"{r.version_name} ({r.version_code}) · {r.get_update_type_display()}",
                         "mandatory": r.is_mandatory}
                        for r in releases if r.channel == channel and r.company_id == company.pk
                    ]
                    for company in companies
                },
            })

        with_hours = services.working_time_branch_ids([b.pk for b in branches])
        history = (
            scoping.release_mappings_for(user)
            .filter(is_active=False)
            .select_related("release", "branch", "created_by", "modified_by")
            .order_by("-modified_on")[:300]
        )
        context.update({
            "apps": apps,
            "companies": companies,
            "multi_company": len(companies) > 1,
            "map_branches": [
                {"id": b.pk, "name": b.name, "code": b.short_code,
                 "company": b.company.name, "company_id": b.company_id,
                 "location": b.location.name if b.location_id else "",
                 "has_working_time": b.pk in with_hours}
                for b in branches
            ],
            # Keyed app -> company, because a release belongs to one company and
            # the picker must not offer another company's build.
            "map_releases": {a["value"]: a["choices"] for a in apps},
            # What each branch runs now, per app -- the picker's "Now on" column.
            "map_state": {
                a["value"]: {
                    str(r["branch"].pk): (
                        f"{r['release'].version_name} · {'own' if r['source'] == 'own' else 'All branches'}"
                        if r["release"] else "No update"
                    )
                    for block in a["blocks"] for r in block["rows"]
                }
                for a in apps
            },
            "history": [
                {
                    "app": m.get_channel_display(),
                    "release": f"{m.release.version_name} ({m.release.version_code})",
                    "target": m.branch.name if m.branch_id else "All branches",
                    "mapped_by": services.person_name(m.created_by),
                    "mapped_on": m.created_on,
                    "removed_by": services.person_name(m.modified_by),
                    "removed_on": m.modified_on,
                }
                for m in history
            ],
            "mapping_urls": {
                "preview": reverse("devices-release-mapping-preview"),
                "save": reverse("devices-release-mapping-save"),
                "remove": reverse("devices-release-mapping-remove", args=[0]),
            },
            "working_time_url": reverse("company-branch-working-time-edit"),
        })
        return context


class ReleaseWriteView(writes.WriteView):
    """JSON {ok, message | errors} for the release screens' writes."""

    page_code = "devices.app_release"

    def invalid(self, errors, code="invalid"):
        return JsonResponse({"ok": False, "code": code, "errors": errors}, status=400)

    def run(self, action, fn):
        if not self.may(action):
            return self.refused()
        try:
            return fn()
        except services.ReleaseInvalid as refused:
            return self.invalid(refused.errors)
        except services.DeviceActionError as refused:
            return self.invalid([{"field": refused.field, "message": refused.message}])


class ReleaseSaveView(ReleaseWriteView):
    """The drawer: create without a pk, edit with one."""

    model = AppRelease

    def post(self, request, *args, **kwargs):
        data = writes._payload(request)
        pk = _int(data.get("pk"))

        def save():
            if pk:
                release = services.update_release(
                    scoping.releases_for(request.user), pk, user=request.user,
                    update_type=data.get("update_type"), download_url=data.get("download_url"),
                    update_note=data.get("update_note"),
                )
                return JsonResponse({"ok": True, "pk": release.pk, "message": f"{release} saved."})
            company_id = _posted_company(request.user, data.get("company"))
            release = services.create_release(
                user=request.user, company_id=company_id, channel=data.get("channel"),
                version_name=data.get("version_name"), version_code=data.get("version_code"),
                update_type=data.get("update_type"), download_url=data.get("download_url"),
                update_note=data.get("update_note"),
            )
            return JsonResponse({
                "ok": True, "pk": release.pk,
                "message": f"{release} added. Map it on App Mapping to send it to tablets.",
            })

        return self.run("update" if pk else "create", save)


class ReleaseWithdrawView(ReleaseWriteView):
    model = AppRelease

    def post(self, request, pk, *args, **kwargs):
        def act():
            release = services.withdraw_release(scoping.releases_for(request.user), pk, user=request.user)
            return JsonResponse({"ok": True, "message": f"{release} withdrawn."})
        return self.run("update", act)


class ReleaseRestoreView(ReleaseWriteView):
    model = AppRelease

    def post(self, request, pk, *args, **kwargs):
        def act():
            release = services.restore_release(scoping.releases_for(request.user), pk, user=request.user)
            return JsonResponse({"ok": True, "message": f"{release} restored."})
        return self.run("update", act)


class MappingWriteView(ReleaseWriteView):
    page_code = "devices.app_release_mapping"
    model = AppReleaseMapping

    def request_parts(self, data):
        """(release, scope, branches, targets) from a posted mapping."""
        user = self.request.user
        release = scoping.releases_for(user).filter(pk=_int(data.get("release"))).first()
        if release is None:
            raise services.DeviceActionError("Choose a release.", "Release")
        scope = data.get("scope")
        if scope not in ReleaseScope.values:
            raise services.DeviceActionError("Choose All branches or selected branches.", "Target")
        branches = list(
            branches_for(user).filter(is_active=True, company_id=release.company_id).order_by("name")
        )
        wanted = {_int(v) for v in (data.get("branches") or [])} - {None}
        targets = [b for b in branches if b.pk in wanted] if scope == ReleaseScope.BRANCH else []
        if scope == ReleaseScope.BRANCH and len(targets) != len(wanted):
            raise services.DeviceActionError("Choose active branches of this company.", "Branches")
        return release, scope, branches, targets


class MappingPreviewView(MappingWriteView):
    """What Save would change -- counts and warnings -- before anything is written."""

    def post(self, request, *args, **kwargs):
        data = writes._payload(request)

        def preview():
            release, scope, branches, targets = self.request_parts(data)
            if scope == ReleaseScope.BRANCH and not targets:
                raise services.DeviceActionError("Tick at least one branch.", "Branches")
            plan = services.mapping_plan(release.company_id, release.channel, release, scope, branches, targets)
            return JsonResponse({"ok": True, "plan": plan})

        return self.run("create", preview)


class MappingSaveView(MappingWriteView):
    def post(self, request, *args, **kwargs):
        data = writes._payload(request)

        def save():
            release, scope, branches, targets = self.request_parts(data)
            plan = services.mapping_plan(release.company_id, release.channel, release, scope, branches, targets)
            # The same warnings the preview showed; saving past them is a
            # deliberate act, not a missed dialog.
            if plan["warnings"] and data.get("confirmed") is not True:
                return self.invalid(
                    [{"field": "", "message": w["message"]} for w in plan["warnings"]], code="confirm",
                )
            written = services.map_release(
                scoping.releases_for(request.user), release.pk, user=request.user,
                scope=scope, branches=branches, targets=targets,
            )
            where = "All branches" if scope == ReleaseScope.ALL else (
                f"{written} branch{'es' if written != 1 else ''}"
            )
            return JsonResponse({"ok": True, "message": f"{release} mapped to {where}."})

        return self.run("create", save)


class MappingRemoveView(MappingWriteView):
    def post(self, request, pk, *args, **kwargs):
        def act():
            mapping = services.unmap_release(
                scoping.release_mappings_for(request.user), pk, user=request.user,
            )
            if mapping.branch_id:
                message = f"{mapping.branch} no longer has its own build; it follows All branches."
            else:
                message = "All-branches release removed."
            return JsonResponse({"ok": True, "message": message})

        return self.run("update", act)


# --- Device Settings -----------------------------------------------------------
#
# One active row per station: the receipt's wording, its logo, the printer's
# habits and the order-number prefix. A tablet downloads the lot after login and
# keeps it, so it can print while the station is offline.


class DeviceSettingsView(DeviceScreenView):
    """Every station, with its settings or the fact that it has none."""

    template_name = "devices/device_settings_list.html"
    page_code = "devices.device_settings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        companies = _companies(user)
        branches = list(
            branches_for(user).filter(is_active=True, company__in=companies)
            .select_related("location", "company").order_by("company__name", "name")
        )
        # octet_length rather than the column: the bytes are deferred on purpose,
        # and a grid only needs to know whether a logo is there and how big.
        rows = {
            row.branch_id: row
            for row in scoping.settings_for(user)
            .filter(is_active=True, branch__in=branches)
            .select_related("created_by", "modified_by")
            .annotate(logo_bytes=Coalesce(
                Func(F("logo"), function="octet_length", output_field=IntegerField()), Value(0)
            ))
        }

        grid, sources = [], []
        for branch in branches:
            row = rows.get(branch.pk)
            grid.append({
                "branch": branch,
                "row": row,
                "logo_bytes": getattr(row, "logo_bytes", 0) if row else 0,
                "logo_url": reverse("devices-settings-logo", args=[row.pk]) if row else "",
                "updated_by": services.person_name(row.modified_by or row.created_by) if row else "",
                "json_id": f"settings-{row.pk}" if row else "",
                "fields_json": _settings_json(row) if row else None,
            })
            if row is not None:
                sources.append({
                    "id": row.pk,
                    "name": f"{branch.name} ({branch.company.short_code})" if len(companies) > 1 else branch.name,
                })

        context.update({
            "rows": grid,
            "companies": companies,
            # A Company column and filter only when there is more than one, so a
            # single-company deployment reads exactly as before.
            "multi_company": len(companies) > 1,
            "settings_stations": [
                {"id": b.pk, "name": b.name} for b in branches if b.pk not in rows
            ],
            "settings_sources": sources,
            # Copy from another station: the values the drawer prefills.
            "settings_source_values": {
                str(row.pk): _settings_json(row, copyable=True) for row in rows.values()
            },
            "settings_branches": [
                # `label` is what the drawer's Station dropdown shows; `name`
                # stays the plain station name for the Copy-to table's column.
                {"id": b.pk, "name": b.name, "code": b.short_code,
                 "label": f"{b.name} — {b.company.short_code}" if len(companies) > 1 else b.name,
                 "company": b.company.name, "company_id": b.company_id,
                 "location": b.location.name if b.location_id else "",
                 "has_settings": b.pk in rows}
                for b in branches
            ],
            "shareable_fields": [
                {"id": name, "name": label} for name, label in services.SETTINGS_SHAREABLE.items()
            ],
            "print_types": [{"id": v, "name": label} for v, label in PrintType.choices],
            "round_off_modes": [{"id": v, "name": label} for v, label in RoundOffMode.choices],
            "round_off_steps": [{"id": str(v), "name": label} for v, label in ROUND_OFF_STEPS],
            "settings_company_note": _company_note(companies),
            "settings_urls": {
                "save": reverse("devices-settings-save"),
                "logo_save": reverse("devices-settings-logo-save", args=[0]),
                "logo_clear": reverse("devices-settings-logo-clear", args=[0]),
                "apply": reverse("devices-settings-apply", args=[0]),
                "deactivate": reverse("devices-settings-deactivate", args=[0]),
            },
            "counts": {
                "stations": len(branches),
                "set": len(rows),
                "missing": len(branches) - len(rows),
                "logos": sum(1 for r in grid if r["logo_bytes"]),
            },
        })
        return context


def _company_note(companies):
    """What the drawer says about the company these settings belong to.

    Settings hang off a station, so the company is never chosen here -- it is
    whichever company owns the station. With several companies on the server
    that is worth saying out loud, because tax and discount type come from it.
    """
    if not companies:
        return ""
    if len(companies) == 1:
        company = companies[0]
        return (
            f"Company: {company.name}. Tax type ({company.get_tax_type_display()}) and "
            f"discount type ({company.get_discount_type_display()}) are the company's, "
            "and reach the tablet with these settings."
        )
    return (
        f"{len(companies)} companies. These settings belong to whichever company owns "
        "the station you pick, and its tax and discount type reach the tablet with them."
    )


def _settings_json(row, *, copyable=False):
    """The record the drawer fills itself from. Never the logo: it has its own
    upload, and 25 KB of base64 in every row of a 95-station grid is not a page."""
    fields = {
        "additional_header_1": row.additional_header_1,
        "additional_header_2": row.additional_header_2,
        "footer_1": row.footer_1,
        "footer_2_arabic": row.footer_2_arabic,
        "paper_feed": str(row.paper_feed),
        "print_type": row.print_type,
        "print_logo": row.print_logo,
        "receipt_copies": str(row.receipt_copies),
        "share_on_whatsapp": row.share_on_whatsapp,
        "round_off_mode": row.round_off_mode,
        "round_off_step": str(row.round_off_step),
        "customer_test_minutes": str(row.customer_test_minutes),
        "cashier_test_minutes": str(row.cashier_test_minutes),
        "header_1": row.header_1,
        "header_2": row.header_2,
    }
    if copyable:
        # Station, code and prefix are never copied: they are what makes a
        # station's receipt its own.
        return fields
    fields.update({
        "pk": row.pk,
        "title": f"{row.branch.name} settings",
        "branch": str(row.branch_id),
        "branch_name": row.branch.name,
        "settings_code": row.settings_code,
        "order_no_prefix": row.order_no_prefix,
    })
    return fields


class SettingsLogoView(PagePermissionMixin, View):
    """The bitmap itself, for the screen's preview.

    Served from the row rather than a media file: it is the only copy, it is
    backed up with the database, and it cannot go missing behind a row that
    says it is there.
    """

    page_code = "devices.device_settings"

    def get(self, request, pk, *args, **kwargs):
        row = (
            scoping.settings_for(request.user).with_logo()
            .filter(pk=pk).values("logo", "logo_name", "logo_changed_on").first()
        )
        if row is None or not row["logo"]:
            raise Http404("No logo")
        response = HttpResponse(bytes(row["logo"]), content_type="image/bmp")
        response["Content-Disposition"] = f'inline; filename="{row["logo_name"] or "logo.bmp"}"'
        if row["logo_changed_on"]:
            response["ETag"] = f'"{int(row["logo_changed_on"].timestamp())}"'
        response["Cache-Control"] = "private, max-age=0, must-revalidate"
        return response


class SettingsWriteView(ReleaseWriteView):
    page_code = "devices.device_settings"
    model = DeviceSettings

    def settings(self):
        return scoping.settings_for(self.request.user)

    def branches(self):
        return branches_for(self.request.user)


class SettingsSaveView(SettingsWriteView):
    """The drawer: add when no pk is posted, edit when there is one."""

    def post(self, request, *args, **kwargs):
        data = writes._payload(request)
        pk = _int(data.get("pk"))

        def save():
            row = services.save_settings(
                self.settings(), self.branches(), user=request.user, pk=pk, data=data,
            )
            return JsonResponse({
                "ok": True, "pk": row.pk,
                "message": f"Settings saved for {row.branch.name}.",
            })

        return self.run("update" if pk else "create", save)


class SettingsLogoSaveView(SettingsWriteView):
    """Its own endpoint because the drawer posts JSON and a file cannot ride
    along. The bytes are stored as they arrive."""

    def post(self, request, pk, *args, **kwargs):
        def save():
            upload = request.FILES.get("logo")
            if upload is None:
                raise services.DeviceActionError("Choose a file to upload.", "Logo")
            row, warnings = services.set_logo(
                self.settings(), pk, user=request.user,
                raw=upload.read(), name=upload.name,
            )
            return JsonResponse({
                "ok": True,
                "message": f"Logo saved for {row.branch.name}. Tablets take it at their next login.",
                "warnings": warnings,
                "logo_url": reverse("devices-settings-logo", args=[row.pk]),
            })

        return self.run("update", save)


class SettingsLogoClearView(SettingsWriteView):
    def post(self, request, pk, *args, **kwargs):
        def act():
            row = services.clear_logo(self.settings(), pk, user=request.user)
            return JsonResponse({"ok": True, "message": f"Logo removed for {row.branch.name}."})

        return self.run("update", act)


class SettingsApplyView(SettingsWriteView):
    """Copy the ticked fields onto other stations' settings."""

    def post(self, request, pk, *args, **kwargs):
        data = writes._payload(request)

        def act():
            wanted = {_int(v) for v in (data.get("branches") or [])} - {None}
            branches = list(self.branches().filter(pk__in=wanted, is_active=True))
            if len(branches) != len(wanted):
                raise services.DeviceActionError("Choose active stations of this company.", "Stations")
            written, skipped = services.apply_settings_to(
                self.settings(), pk, user=request.user,
                fields=[str(f) for f in (data.get("fields") or [])], branches=branches,
            )
            message = f"Copied to {written} station{'s' if written != 1 else ''}."
            if skipped:
                message += f" {skipped} had no settings of their own and were skipped."
            return JsonResponse({"ok": True, "message": message})

        return self.run("update", act)


class SettingsDeactivateView(SettingsWriteView):
    def post(self, request, pk, *args, **kwargs):
        def act():
            row = services.deactivate_settings(self.settings(), pk, user=request.user)
            return JsonResponse({
                "ok": True,
                "message": f"{row.branch.name} has no settings now. Its tablets keep the last copy "
                           "they downloaded until new settings are added.",
            })

        return self.run("update", act)

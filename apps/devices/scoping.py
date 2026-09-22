"""Which device rows a user may see.

Device carries a company, so it filters directly. Mappings, the status log and
sessions hang off a device, so they follow it.
"""

from apps.devices.models import (
    AppRelease,
    AppReleaseMapping,
    BillContinuity,
    Device,
    DeviceMapping,
    DeviceSettings,
    DeviceStatusLog,
)
from apps.portal.session_models import AppSession
from core.scoping import scoped_to


def devices_for(user):
    return scoped_to(Device.objects.all(), user)


def mappings_for(user):
    return DeviceMapping.objects.filter(device__in=devices_for(user))


def status_log_for(user):
    return DeviceStatusLog.objects.filter(device__in=devices_for(user))


def device_sessions_for(user):
    """App sessions on this user's devices -- the source of who is logged in
    where, and on which app version."""
    return AppSession.objects.filter(device__in=devices_for(user))


def releases_for(user):
    return scoped_to(AppRelease.objects.all(), user)


def release_mappings_for(user):
    # Carries its own company (a copy held true by a composite foreign key), so
    # it filters directly rather than through the release.
    return scoped_to(AppReleaseMapping.objects.all(), user)


def settings_for(user):
    # No company column of its own: it is the branch's.
    return scoped_to(DeviceSettings.objects.all(), user, field="branch__company")


def bill_counters_for(user):
    return BillContinuity.objects.filter(device__in=devices_for(user))

"""Signing in.

The checks and their order come from design/03-login.md section 4. Two rules set
that order: nothing about an account is revealed until the password is proved,
and cheap checks come first.

This module owns the rules. Views and API endpoints call it; they never repeat a
check themselves.
"""

from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone

from apps.devices.models import DeviceStatus
from apps.portal.session_models import AppSession, LogoutReason
from core.enums import ApprovalStatus, Channel
from core.models import User

# design/03-login.md decision 4
MAX_FAILED_ATTEMPTS = 15
LOCKOUT_DURATION = timedelta(minutes=5)

# The same message for an unknown username and a wrong password, so the login
# page cannot be used to find out which usernames exist.
GENERIC_CREDENTIALS_MESSAGE = "Wrong username or password."

# check_credentials hashes against this for an unknown username, so that
# branch takes about as long as a known username's real check_password call
# does -- without it, the *message* is identical either way but the
# *response time* isn't (an unknown username returns immediately, a wrong
# password only after a deliberately slow hash comparison), which is exactly
# the username-enumeration channel the identical message was meant to close.
# Computed once at import, not per request -- it's a fixed value nothing
# ever needs to match against for real.
_DUMMY_PASSWORD_HASH = make_password("no-such-account-dummy-hash")


class LoginRefused(Exception):
    """A login the server will not grant. `code` is the contract with the apps
    (design/03-login.md section 9.2); `message` is what the person reads."""

    def __init__(self, code, message, status=403):
        super().__init__(code)
        self.code = code
        self.message = message
        self.status = status


def _locked(user, now):
    return user.locked_until is not None and user.locked_until > now


def _register_failure(user, now):
    """Count a wrong password and lock the account once it runs out of tries."""
    user.failed_login_count += 1
    if user.failed_login_count >= MAX_FAILED_ATTEMPTS:
        user.locked_until = now + LOCKOUT_DURATION
        user.failed_login_count = 0
    user.save(update_fields=["failed_login_count", "locked_until", "modified_on"])


def _clear_failures(user):
    if user.failed_login_count or user.locked_until:
        user.failed_login_count = 0
        user.locked_until = None
        user.save(update_fields=["failed_login_count", "locked_until", "modified_on"])


def check_credentials(username, password, *, now=None):
    """Checks 1 and 2: lockout, then username and password.

    Returns the user. Everything past this point may speak plainly about the
    account, because the password has been proved.
    """
    now = now or timezone.now()
    username = User.normalize_username(username)

    user = User.objects.filter(username=username).first()
    if user is None:
        # Same message as a wrong password, and now roughly the same timing
        # too -- check_password against the dummy hash costs about what the
        # real check below costs, so the response time itself doesn't
        # reveal that this username never existed.
        check_password(password, _DUMMY_PASSWORD_HASH)
        raise LoginRefused("invalid_credentials", GENERIC_CREDENTIALS_MESSAGE, status=401)

    if _locked(user, now):
        minutes = max(1, int((user.locked_until - now).total_seconds() // 60) + 1)
        raise LoginRefused(
            "account_locked",
            f"Too many attempts. Try again in {minutes} minutes.",
            status=429,
        )

    if not user.check_password(password):
        _register_failure(user, now)
        raise LoginRefused("invalid_credentials", GENERIC_CREDENTIALS_MESSAGE, status=401)

    return user


def check_account(user, channel):
    """Checks 3 to 6: company, user, channel, employee.

    Blocked is a person and active is an account: `employee.is_blocked` is set by
    HR, `user.is_active` by an administrator. A user with no employee skips the
    last check, which is right -- there is no person in HR to block.
    """
    company = getattr(user, "company", None)
    if company is not None and not company.is_active:
        raise LoginRefused("company_inactive", "This account is not active.")

    if not user.is_active or user.approval_status != ApprovalStatus.APPROVED:
        raise LoginRefused("user_not_approved", "This account is not active.")

    if not user.can_use(channel):
        raise LoginRefused("wrong_channel", "You are not allowed to use this app.")

    employee = getattr(user, "employee", None)
    if employee is not None and getattr(employee, "is_blocked", False):
        raise LoginRefused("user_blocked", "You are blocked. Contact HR.")


def open_session(user, channel, *, device=None, branch=None,
                 app_version="", ip_address=None, user_agent="", now=None):
    """Check 11: settle other sessions, then record this one.

    Web closes the user's older sessions -- newest wins (design/03-login.md
    decision 1). Apps refuse a login from another device before reaching here,
    and a second login on the same device replaces the old session.
    """
    now = now or timezone.now()

    with transaction.atomic():
        (
            AppSession.objects
            .select_for_update()
            .filter(user=user, channel=channel, logged_out_at__isnull=True)
            .update(logged_out_at=now, logout_reason=LogoutReason.REPLACED)
        )
        return AppSession.objects.create(
            user=user,
            channel=channel,
            device=device,
            branch=branch,
            logged_in_at=now,
            last_seen_at=now,
            app_version=app_version,
            ip_address=ip_address,
            user_agent=user_agent[:255],
        )


def sign_in_web(username, password, *, ip_address=None, user_agent="", now=None):
    """The whole web sign-in: checks 1 to 6, then 11w.

    Raises LoginRefused, or returns (user, session).
    """
    now = now or timezone.now()
    user = check_credentials(username, password, now=now)
    check_account(user, Channel.WEB)
    _clear_failures(user)

    session = open_session(
        user, Channel.WEB,
        ip_address=ip_address, user_agent=user_agent, now=now,
    )
    user.last_login = now
    user.save(update_fields=["last_login"])
    return user, session


def sign_in_employee(username, password, installation_id, *, ip_address=None, user_agent="", now=None):
    """The Employee app's whole sign-in: checks 1 to 6, then 7-8, then 11.

    Runs the registered/approved/not-blocked/not-retired subset of the device
    steps design/03-login.md section 4 specifies for apps -- decided with the
    client 22 Sep 2026, reversing the 21 Sep 2026 decision that this channel
    skipped every device step. Deliberately **not** run: step 9 (branch
    mapping) and the device_settings check sign_in_operator runs after it --
    the Employee app runs on staff's own phones, not a station till, and
    "which branch" comes from the duty roster (apps.crew.services), never a
    device mapping. Step 10 (refuse a session open on a *different* device)
    is also not run here -- out of scope for this pass, unlike sign_in_operator.

    apps.devices.services is imported locally, not at module level: same
    circular-import reason documented on sign_in_operator below.

    Returns (user, session). Raises LoginRefused for every failure.
    """
    from apps.devices import services as devices_services

    now = now or timezone.now()
    user = check_credentials(username, password, now=now)
    check_account(user, Channel.EMPLOYEE)
    if user.employee_id is None:
        # A user with the employee channel allowed but no linked employee
        # record has nothing for this app to show -- same generic message as
        # a wrong password, so it does not leak which usernames exist.
        raise LoginRefused("invalid_credentials", GENERIC_CREDENTIALS_MESSAGE, status=401)
    _clear_failures(user)

    device = devices_services.device_for_installation(installation_id)
    if device is None:
        raise LoginRefused("device_not_registered", "Setting up this device…", status=409)
    if device.status == DeviceStatus.PENDING:
        raise LoginRefused("device_pending_approval", "Waiting for approval.", status=202)
    if device.status == DeviceStatus.BLOCKED:
        raise LoginRefused("device_blocked", "This device is blocked.", status=403)
    if device.status == DeviceStatus.RETIRED:
        raise LoginRefused("device_retired", "This device was replaced.", status=403)

    session = open_session(
        user, Channel.EMPLOYEE, device=device,
        ip_address=ip_address, user_agent=user_agent, now=now,
    )
    user.last_login = now
    user.save(update_fields=["last_login"])
    return user, session


def sign_in_operator(username, password, installation_id, *, ip_address=None, user_agent="", now=None):
    """The Operator (RMS till) app's whole sign-in: checks 1 to 11, unlike
    the Employee app's 1-6 -- this channel is a station till, so the device
    steps design/03-login.md section 4 specifies for apps all apply: the
    device must be registered, approved, mapped to a branch, and that branch
    must have its receipt/print settings configured (device_settings_not_done
    -- new, the legacy's StatusFlag 11, which section 9.2's refusal table had
    no equivalent for).

    apps.devices.services is imported locally, not at module level: that
    module itself imports close_sessions_for_device from here, so importing
    it back at the top of this file would be circular.

    Returns (user, session, device, branch). Raises LoginRefused for every
    failure, exactly like sign_in_web/sign_in_employee.
    """
    from apps.devices import services as devices_services

    now = now or timezone.now()
    user = check_credentials(username, password, now=now)
    check_account(user, Channel.OPERATOR)
    if user.employee_id is None:
        # The legacy proc's own query INNER JOINs HrmsEmployee -- an account
        # with no linked employee could never match it at all. Same generic
        # message as a wrong password, as sign_in_employee already does.
        raise LoginRefused("invalid_credentials", GENERIC_CREDENTIALS_MESSAGE, status=401)
    _clear_failures(user)

    device = devices_services.device_for_installation(installation_id)
    if device is None:
        raise LoginRefused("device_not_registered", "Setting up this device…", status=409)
    if device.status == DeviceStatus.PENDING:
        raise LoginRefused("device_pending_approval", "Waiting for approval.", status=202)
    if device.status == DeviceStatus.BLOCKED:
        raise LoginRefused("device_blocked", "This device is blocked.", status=403)
    if device.status == DeviceStatus.RETIRED:
        raise LoginRefused("device_retired", "This device was replaced.", status=403)

    branch = device.current_branch
    if branch is None:
        raise LoginRefused("device_not_mapped", "This device has no station.", status=409)

    if devices_services.settings_payload(branch) is None:
        raise LoginRefused(
            "device_settings_not_done",
            "This station's receipt settings are not set up yet.",
            status=409,
        )

    # Step 10: an open session on a *different* device is refused; the same
    # device re-logging in is allowed through to open_session below, which
    # replaces its own prior row exactly as web/employee already do.
    other_device_open = (
        AppSession.objects
        .filter(user=user, channel=Channel.OPERATOR, logged_out_at__isnull=True)
        .exclude(device=device)
        .exists()
    )
    if other_device_open:
        raise LoginRefused("session_active_elsewhere", "Logged in on another device.", status=409)

    session = open_session(
        user, Channel.OPERATOR, device=device, branch=branch,
        ip_address=ip_address, user_agent=user_agent, now=now,
    )
    user.last_login = now
    user.save(update_fields=["last_login"])
    return user, session, device, branch


def close_session(session, reason, *, now=None):
    """Closing is final. A session is never reopened; a new sign-in always
    creates a new row (design/03-login.md section 7.3)."""
    if session.logged_out_at is not None:
        return session
    session.logged_out_at = now or timezone.now()
    session.logout_reason = reason
    session.save(update_fields=["logged_out_at", "logout_reason", "modified_on"])
    return session


def close_sessions_for_user(user, reason, *, channel=None, now=None):
    """Used by force logout, by blocking an employee, and by a password change."""
    sessions = AppSession.objects.filter(user=user, logged_out_at__isnull=True)
    if channel:
        sessions = sessions.filter(channel=channel)
    return sessions.update(logged_out_at=now or timezone.now(), logout_reason=reason)


def close_sessions_for_device(device, reason, *, now=None):
    """Used when a device is blocked or moved to another station."""
    return (
        AppSession.objects
        .filter(device=device, logged_out_at__isnull=True)
        .update(logged_out_at=now or timezone.now(), logout_reason=reason)
    )

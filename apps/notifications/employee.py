"""Pushes about people, not orders (owner, 30 Oct 2026): the employee app is told
when their leave is decided, their duty roster changes from today on, or a
tablet marks their attendance; a new leave request tells the station's leave
approvers (employee app) and the company's managers (manager app).

Same outbox as services.notify -- one Notification per recipient session,
written in the caller's transaction and queued after commit; only open,
signed-in sessions holding a token -- but the recipients are people:
`Audience.EMPLOYEE` is the employee-app sessions of one user (any phone
they are signed in on). Every employee-app push uses the ringing
`employee_alerts` channel (owner: all ring).
"""

import datetime

from django.db import transaction
from django.utils import timezone

from apps.notifications.models import Action, AndroidChannel, Audience, Notification, SubjectType
from apps.notifications.services import queue
from apps.portal.auth import push_sessions
from core.enums import Channel
from core.ids import uuid7
from core.timezones import zone_for


def _employee_sessions(user_ids, now):
    return list(push_sessions(now=now).filter(channel=Channel.EMPLOYEE, user_id__in=user_ids)
                .values_list("pk", flat=True))


def _manager_sessions(company_id, now):
    return list(push_sessions(now=now).filter(channel=Channel.MANAGER, user__employee__company_id=company_id)
                .values_list("pk", flat=True))


def _write(company_id, subject_type, subject_id, action, sends, *, tag, data, now):
    """`sends`: (audience, session ids, title, body, android channel) per group."""
    rows = []
    for audience, session_ids, title, body, channel in sends:
        for session_id in session_ids:
            pk = uuid7()
            rows.append(Notification(
                id=pk, company_id=company_id, subject_type=subject_type, subject_id=subject_id, action=action,
                audience=audience, session_id=session_id, title=title[:120], body=body[:200], tag=tag[:60],
                android_channel=channel,
                data={"notification_id": str(pk), "subject_type": subject_type, "subject_id": str(subject_id),
                      "action": action, **{k: str(v) if v is not None else "" for k, v in data.items()}},
                next_attempt_at=now,
            ))
    if rows:
        Notification.objects.bulk_create(rows)
        ids = [str(row.pk) for row in rows]
        transaction.on_commit(lambda: queue(ids))
    return rows


def _user_id(employee):
    user = getattr(employee, "user", None)
    return user.pk if user is not None else None


def date_range(start, end):
    """'2 Nov', '2–4 Nov', '30 Nov–2 Dec', '30 Dec 2026–2 Jan 2027'."""
    if start == end:
        return f"{start.day} {start:%b}"
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day}–{end.day} {end:%b}"
    if start.year == end.year:
        return f"{start.day} {start:%b}–{end.day} {end:%b}"
    return f"{start.day} {start:%b} {start.year}–{end.day} {end:%b} {end.year}"


# -- Leave ----------------------------------------------------------------------------

def leave_raised(row, now=None):
    """A new request: the leave approvers of its stations (employee app, not the
    requester) and every manager of the company (manager app)."""
    from apps.company.models import AuthorityType, BranchApprovalAuthority

    now = now or timezone.now()
    approver_users = set(
        BranchApprovalAuthority.objects
        .filter(authority_type=AuthorityType.LEAVE_REQUEST, branch__in=row.stations.all(),
                branch__company=row.company_id, employee__user__isnull=False)
        .exclude(employee_id=row.employee_id)
        .values_list("employee__user__id", flat=True)
    )
    body = f"{row.employee.full_name} · {date_range(row.from_date, row.to_date)} · {row.leave_type.name}"
    return _write(row.company_id, SubjectType.LEAVE_REQUEST, row.pk, Action.RAISED, [
        (Audience.EMPLOYEE, _employee_sessions(approver_users, now), "New leave request", body,
         AndroidChannel.EMPLOYEE_ALERTS),
        (Audience.MANAGER, _manager_sessions(row.company_id, now), "New leave request", body,
         AndroidChannel.APPROVALS_NEW),
    ], tag=f"leave_request:{row.pk}", data={"leave_request_id": row.pk, "employee_id": row.employee_id}, now=now)


def leave_decided(row, now=None):
    """Approved or rejected: the employee who asked."""
    now = now or timezone.now()
    approved = row.status == "approved"
    body = f"{date_range(row.from_date, row.to_date)} · {row.leave_type.name}"
    if row.decision_remarks:
        body += f" · {row.decision_remarks}"
    return _write(row.company_id, SubjectType.LEAVE_REQUEST, row.pk,
                  Action.APPROVED if approved else Action.REJECTED, [
                      (Audience.EMPLOYEE, _employee_sessions([_user_id(row.employee)], now),
                       "Leave approved" if approved else "Leave rejected", body, AndroidChannel.EMPLOYEE_ALERTS),
                  ], tag=f"leave_request:{row.pk}", data={"leave_request_id": row.pk}, now=now)


# -- Duty roster ------------------------------------------------------------------------

def roster_updated(employee, dates, now=None):
    """One save changed these days of the employee's roster. Only days from
    today on (company time) count; none -- nothing is sent."""
    now = now or timezone.now()
    today = now.astimezone(zone_for(employee.company)).date()
    days = sorted(d for d in dates if isinstance(d, datetime.date) and d >= today)
    if not days:
        return []
    return _write(employee.company_id, SubjectType.DUTY_ROSTER, uuid7(), Action.UPDATED, [
        (Audience.EMPLOYEE, _employee_sessions([_user_id(employee)], now), "Duty roster updated",
         date_range(days[0], days[-1]), AndroidChannel.EMPLOYEE_ALERTS),
    ], tag=f"duty_roster:{employee.pk}",
        data={"employee_id": employee.pk, "from_date": days[0].isoformat(), "to_date": days[-1].isoformat()},
        now=now)


def roster_saves(changes, now=None):
    """`changes`: {employee: dates} from one save -- one push per employee."""
    rows = []
    for employee, dates in changes.items():
        rows += roster_updated(employee, dates, now=now)
    return rows


# -- Attendance -------------------------------------------------------------------------

def attendance_marked(row, now=None):
    """A tablet (operator app QR scan) recorded a punch for the employee."""
    now = now or timezone.now()
    employee = row.employee
    zone = zone_for(employee.company)
    station = row.rms_branch.name if row.rms_branch_id else ""
    at = row.rms_scan_time.astimezone(zone).strftime("%H:%M") if row.rms_scan_time else ""
    title = "Punched in" if row.punch_type == "punch_in" else "Punched out"
    return _write(employee.company_id, SubjectType.ATTENDANCE, row.pk, Action.MARKED, [
        (Audience.EMPLOYEE, _employee_sessions([_user_id(employee)], now), title,
         " · ".join(part for part in (station, at) if part), AndroidChannel.EMPLOYEE_ALERTS),
    ], tag=f"attendance:{row.pk}",
        data={"attendance_id": row.pk, "punch_type": row.punch_type, "branch_id": row.rms_branch_id}, now=now)

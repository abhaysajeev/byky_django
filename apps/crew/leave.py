"""Leave: an employee asks, a manager or a branch's Leave Request approver
sanctions, the duty roster shows it.

Built from the employee app SRS section 10, the manager app SRS section 3 and
the owner's decisions of 15 Oct 2026 -- there is no legacy leave to port. The
rules:

* An employee applies in the employee app for whole days from today on: a
  type, from / to date, a reason. Days are calendar days, both ends
  included. No balance is kept. A request may not overlap the same
  employee's pending or approved one.
* Only the applicant cancels, and only while pending.
* Managers (manager app, the whole company), the web (page permission
  `crew.leave_request` approve) and the Leave Request approvers of the
  request's stations (employee app) approve or reject. An approver never
  decides their own request -- a manager does.
* Approved is final. A first rejection may be changed once (the manager app's
  "Edit Status"), to approved or rejected.
* An approval makes every day of the range an On Leave roster day linked to
  the request, replacing what the day held.

Every change is written to the status log.
"""

import datetime

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.company.models import AuthorityType, BranchApprovalAuthority
from apps.crew.models import (
    LEAVE_LIVE,
    DutyRoster,
    DutyRosterDayType,
    Employee,
    LeaveRequest,
    LeaveStatus,
    LeaveStatusLog,
    LeaveType,
)
from core.enums import Channel
from core.timezones import business_date_for

PAGE_SIZE = 50


class LeaveRefused(Exception):
    """A leave action the server will not take. `code` is the contract with
    the apps; `message` is what the person reads."""

    def __init__(self, code, message, status=409):
        super().__init__(code)
        self.code, self.message, self.status = code, message, status


def days_between(from_date, to_date):
    """Calendar days, both ends included."""
    return (to_date - from_date).days + 1


def _dates(row):
    return [row.from_date + datetime.timedelta(days=i) for i in range(row.days)]


def _name(user):
    if user is None:
        return ""
    employee = getattr(user, "employee", None)
    return employee.full_name if employee is not None else (user.display_name or user.username)


def _log(row, user, channel, at, remarks=""):
    LeaveStatusLog.objects.create(leave_request=row, status=row.status, at=at, by=user, by_name=_name(user),
                                  channel=channel, remarks=remarks, created_by=user, modified_by=user)


def _overlap(employee, from_date, to_date, statuses, exclude=None):
    rows = LeaveRequest.objects.filter(employee=employee, status__in=statuses,
                                       from_date__lte=to_date, to_date__gte=from_date)
    if exclude is not None:
        rows = rows.exclude(pk=exclude.pk)
    return rows.order_by("from_date").first()


def _overlap_refusal(other):
    return LeaveRefused(
        "leave_overlaps",
        f"These dates overlap {other.get_status_display().lower()} leave {other.reference} "
        f"({other.from_date:%d %b} - {other.to_date:%d %b}).",
    )


def stations_for(employee, from_date, to_date):
    """Where a request is decided: the stations the employee is rostered
    Working at during the leave, plus their home branch."""
    ids = set(
        DutyRoster.objects
        .filter(employee=employee, date__range=(from_date, to_date), day_type=DutyRosterDayType.WORKING)
        .values_list("branch_id", flat=True)
    )
    if employee.branch_id:
        ids.add(employee.branch_id)
    return ids


# -- The applicant ---------------------------------------------------------------------

def _applied_at(values, now):
    """The phone's submit time when it sent one, never later than now (a fast
    phone clock cannot post-date a request); else the time it reached us."""
    sent = values.get("applied_at")
    return now if sent is None else min(sent, now)


def submit(user, values, now=None):
    """(row, created) -- created is False when this sync_id was already stored."""
    employee = user.employee
    company = employee.company
    sync_id = values["sync_id"]
    existing = LeaveRequest.objects.filter(pk=sync_id).first()
    if existing is not None:
        if existing.employee_id != employee.pk:
            raise LeaveRefused("sync_id_conflict", "That sync_id is already used.")
        return existing, False

    leave_type = LeaveType.objects.filter(pk=values["leave_type_id"], company=company, is_active=True).first()
    if leave_type is None:
        raise LeaveRefused("unknown_leave_type", "No such leave type.", status=400)
    from_date, to_date = values["from_date"], values["to_date"]
    now = now or timezone.now()
    if from_date < business_date_for(company, now):
        raise LeaveRefused("leave_in_past", "Leave can start today at the earliest.", status=400)
    if to_date < from_date:
        raise LeaveRefused("invalid_dates", "To date must be on or after from date.", status=400)

    try:
        with transaction.atomic():
            # One employee's requests are decided one at a time: two submits at
            # once cannot both find no overlap.
            Employee.objects.select_for_update().filter(pk=employee.pk).first()
            other = _overlap(employee, from_date, to_date, LEAVE_LIVE)
            if other is not None:
                raise _overlap_refusal(other)
            row = LeaveRequest.objects.create(
                id=sync_id, company=company, employee=employee, leave_type=leave_type,
                from_date=from_date, to_date=to_date, days=days_between(from_date, to_date),
                reason=(values.get("reason") or "").strip(), applied_at=_applied_at(values, now),
                created_by=user, modified_by=user,
            )
            row.refresh_from_db(fields=["number"])
            row.stations.set(stations_for(employee, from_date, to_date))
            _log(row, user, Channel.EMPLOYEE, now)
    except IntegrityError:
        existing = LeaveRequest.objects.filter(pk=sync_id, employee=employee).first()
        if existing is None:
            raise
        return existing, False
    return row, True


def cancel(user, leave_id, now=None):
    """The applicant withdraws a pending request."""
    employee = user.employee
    now = now or timezone.now()
    with transaction.atomic():
        row = LeaveRequest.objects.select_for_update().filter(pk=leave_id, company=employee.company_id).first()
        if row is None:
            raise LeaveRefused("unknown_leave", "No such leave request.", status=404)
        if row.employee_id != employee.pk:
            raise LeaveRefused("not_your_leave", "You can cancel only your own leave request.", status=403)
        if row.status != LeaveStatus.PENDING:
            raise LeaveRefused("leave_not_pending", f"This request is already {row.get_status_display().lower()}.")
        row.status, row.cancelled_at, row.modified_by = LeaveStatus.CANCELLED, now, user
        row.save(update_fields=["status", "cancelled_at", "modified_by", "modified_on"])
        _log(row, user, Channel.EMPLOYEE, now)
    return row


def my_requests(employee):
    return LeaveRequest.objects.filter(employee=employee)


# -- The approvers ---------------------------------------------------------------------

def approver_branches(employee):
    """The stations where this employee is a Leave Request approver."""
    return set(
        BranchApprovalAuthority.objects
        .filter(employee=employee, authority_type=AuthorityType.LEAVE_REQUEST, branch__company=employee.company_id)
        .values_list("branch_id", flat=True)
    )


def is_approver(employee, channel):
    return channel == Channel.MANAGER or bool(approver_branches(employee))


def decidable(user, channel, company_ids=None):
    """The requests this user may see and decide: the company's in the manager
    app; the given companies' on the web; in the employee app those at the
    stations the user approves for, never their own."""
    rows = LeaveRequest.objects.all()
    if channel == Channel.WEB:
        return rows.filter(company_id__in=company_ids)
    employee = user.employee
    rows = rows.filter(company=employee.company_id)
    if channel == Channel.MANAGER:
        return rows
    branches = approver_branches(employee)
    if not branches:
        raise LeaveRefused("not_a_leave_approver", "You do not approve leave.", status=403)
    ids = LeaveRequest.objects.filter(stations__in=branches).values("pk")
    return rows.filter(pk__in=ids).exclude(employee=employee)


def is_own(row, user):
    employee = getattr(user, "employee", None)
    return employee is not None and row.employee_id == employee.pk


def can_decide(row, user, channel):
    """Waiting, or a first rejection not yet changed -- and not the decider's
    own request, unless a manager decides it in the manager app."""
    open_ = row.status == LeaveStatus.PENDING or (row.status == LeaveStatus.REJECTED and not row.status_edited)
    return open_ and (channel == Channel.MANAGER or not is_own(row, user))


def decide(user, channel, leave_id, approve, remarks="", company_ids=None, now=None):
    """Approve or reject. Returns (row, working days the approval replaced)."""
    now = now or timezone.now()
    with transaction.atomic():
        row = LeaveRequest.objects.select_for_update().select_related("employee").filter(pk=leave_id).first()
        # Checked first: an approver's own request is outside what they decide.
        if row is not None and is_own(row, user) and channel != Channel.MANAGER:
            raise LeaveRefused("own_leave", "Your own leave is decided by a manager.", status=403)
        if row is None or not decidable(user, channel, company_ids).filter(pk=leave_id).exists():
            raise LeaveRefused("unknown_leave", "No such leave request.", status=404)
        if row.status == LeaveStatus.REJECTED and not row.status_edited:
            row.status_edited = True
        elif row.status != LeaveStatus.PENDING:
            raise LeaveRefused("leave_decided", f"This request is already {row.get_status_display().lower()}.")

        replaced = 0
        if approve:
            Employee.objects.select_for_update().filter(pk=row.employee_id).first()
            other = _overlap(row.employee, row.from_date, row.to_date, [LeaveStatus.APPROVED], exclude=row)
            if other is not None:
                raise _overlap_refusal(other)
        row.status = LeaveStatus.APPROVED if approve else LeaveStatus.REJECTED
        row.decided_by, row.decided_at, row.decided_channel = user, now, channel
        row.decision_remarks, row.modified_by = (remarks or "").strip(), user
        row.save(update_fields=["status", "status_edited", "decided_by", "decided_at", "decided_channel",
                                "decision_remarks", "modified_by", "modified_on"])
        if approve:
            replaced = _mark_roster(row, user)
        _log(row, user, channel, now, row.decision_remarks)
    return row, replaced


def working_days_in(row):
    """Working roster days in the request's range now -- what an approval
    would turn into leave."""
    return DutyRoster.objects.filter(employee_id=row.employee_id, date__range=(row.from_date, row.to_date),
                                     day_type=DutyRosterDayType.WORKING).count()


def _mark_roster(row, user):
    """Every day of an approved request becomes On Leave, linked to it."""
    replaced = working_days_in(row)
    days = DutyRoster.objects.filter(employee_id=row.employee_id, date__range=(row.from_date, row.to_date))
    held = set(days.values_list("date", flat=True))
    days.update(day_type=DutyRosterDayType.ON_LEAVE, branch=None, shift1_start=None, shift1_end=None,
                shift2_start=None, shift2_end=None, remarks="", leave_request=row, modified_by=user,
                modified_on=timezone.now())
    DutyRoster.objects.bulk_create([
        DutyRoster(employee_id=row.employee_id, date=day, day_type=DutyRosterDayType.ON_LEAVE, leave_request=row,
                   created_by=user, modified_by=user)
        for day in _dates(row) if day not in held
    ])
    return replaced


# -- Lists -----------------------------------------------------------------------------

def filtered(rows, *, status=None, state_id=None, branch_id=None, leave_type_id=None, from_date=None,
             to_date=None, search=""):
    """A request list narrowed by the screen's filters. Dates match a leave
    that overlaps the range."""
    if status:
        rows = rows.filter(status=status)
    if state_id:
        rows = rows.filter(pk__in=LeaveRequest.objects.filter(stations__location__state_id=state_id).values("pk"))
    if branch_id:
        rows = rows.filter(pk__in=LeaveRequest.objects.filter(stations=branch_id).values("pk"))
    if leave_type_id:
        rows = rows.filter(leave_type_id=leave_type_id)
    if from_date:
        rows = rows.filter(to_date__gte=from_date)
    if to_date:
        rows = rows.filter(from_date__lte=to_date)
    text = (search or "").strip()
    if text:
        rows = rows.filter(Q(employee__employee_code__icontains=text) | Q(employee__first_name__icontains=text)
                           | Q(employee__last_name__icontains=text))
    return rows


def status_counts(rows):
    counts = {status: 0 for status in LeaveStatus.values}
    for status in rows.values_list("status", flat=True):
        counts[status] += 1
    counts["all"] = sum(counts.values())
    return counts


def with_details(rows):
    return (rows.select_related("employee__designation", "leave_type", "decided_by__employee")
            .prefetch_related("stations__location__state", "log").order_by("-applied_at"))


def pending_count(company_ids, *, state_id=None, branch_id=None):
    """Pending requests, for the manager app's badges."""
    rows = filtered(LeaveRequest.objects.filter(company_id__in=company_ids), status=LeaveStatus.PENDING,
                    state_id=state_id, branch_id=branch_id)
    return rows.count()

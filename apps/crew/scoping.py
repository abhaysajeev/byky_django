"""Which crew rows a user may see.

Designation and Employee carry a company, so they filter directly. Everything
else hangs off an employee, so it follows.
"""

from apps.crew.models import (
    Attendance,
    Designation,
    DutyRoster,
    Employee,
    EmployeeAddress,
    EmployeeBlockLog,
)
from core.scoping import scoped_to


def designations_for(user):
    return scoped_to(Designation.objects.all(), user)


def employees_for(user):
    return scoped_to(Employee.objects.all(), user)


def addresses_for(user):
    return EmployeeAddress.objects.filter(employee__in=employees_for(user))


def block_log_for(user):
    return EmployeeBlockLog.objects.filter(employee__in=employees_for(user))


def attendance_for(user):
    return Attendance.objects.filter(employee__in=employees_for(user))


def duty_roster_for(user):
    return DutyRoster.objects.filter(employee__in=employees_for(user))

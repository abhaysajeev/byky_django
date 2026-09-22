from django.urls import path

from apps.crew import views, writes

urlpatterns = [
    # Screens
    path("employee/list/", views.EmployeeListView.as_view(), name="crew-employee-list"),
    path("designation/list/", views.DesignationListView.as_view(), name="crew-designation-list"),
    path("address/list/", views.EmployeeAddressListView.as_view(), name="crew-address-list"),
    path("block-unblock/", views.BlockUnblockView.as_view(), name="crew-block-unblock"),
    path("attendance/list/", views.AttendanceListView.as_view(), name="crew-attendance-list"),
    path("duty-roster/", views.DutyRosterListView.as_view(), name="crew-duty-roster-list"),
    path("privileges/", views.CrewPrivilegeView.as_view(), name="crew-privileges"),

    # Writes
    path("employee/save/", writes.EmployeeSaveView.as_view(), name="crew-employee-save"),
    path("employee/<int:pk>/delete/", views.EmployeeDelete.as_view(), name="crew-employee-delete"),
    path("employee/block/", writes.EmployeeBlockView.as_view(), name="crew-employee-block"),
    path("designation/save/", views.DesignationSave.as_view(), name="crew-designation-save"),
    path("designation/<int:pk>/delete/", views.DesignationDelete.as_view(), name="crew-designation-delete"),
    path("address/save/", views.AddressSave.as_view(), name="crew-address-save"),
    path("address/<int:pk>/delete/", views.AddressDelete.as_view(), name="crew-address-delete"),

    # Duty Roster
    path("duty-roster/branch-shifts/", writes.DutyRosterBranchShiftsView.as_view(), name="crew-duty-roster-branch-shifts"),
    path("duty-roster/save-week/", writes.DutyRosterSaveWeekView.as_view(), name="crew-duty-roster-save-week"),
    path("duty-roster/assign-day/", writes.DutyRosterAssignDayView.as_view(), name="crew-duty-roster-assign-day"),
    path("duty-roster/remove-day/", writes.DutyRosterRemoveDayView.as_view(), name="crew-duty-roster-remove-day"),
    path("duty-roster/import/", writes.DutyRosterImportView.as_view(), name="crew-duty-roster-import"),
]

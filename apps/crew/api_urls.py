"""/api/v1/{app}/attendance/... and /leave/... for the three apps. `{app}` is
checked by the URL converter in config/urls.py; each view narrows it to the
apps it serves."""

from django.urls import path

from apps.crew import api, leave_api

urlpatterns = [
    path("attendance/mark", api.AttendanceMarkView.as_view(), name="api-app-attendance-mark"),
    path("attendance/history", api.AttendanceHistoryView.as_view(), name="api-app-attendance-history"),
    path("leave/types", leave_api.LeaveTypesView.as_view(), name="api-app-leave-types"),
    path("leave/submit", leave_api.LeaveSubmitView.as_view(), name="api-app-leave-submit"),
    path("leave/my", leave_api.MyLeaveView.as_view(), name="api-app-leave-my"),
    path("leave/cancel", leave_api.LeaveCancelView.as_view(), name="api-app-leave-cancel"),
    path("leave/requests", leave_api.LeaveRequestsView.as_view(), name="api-app-leave-requests"),
    path("leave/approve", leave_api.LeaveApproveView.as_view(), name="api-app-leave-approve"),
    path("leave/reject", leave_api.LeaveRejectView.as_view(), name="api-app-leave-reject"),
    path("attendance/team", api.TeamAttendanceView.as_view(), name="api-app-attendance-team"),
]

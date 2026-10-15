"""/api/v1/{app}/attendance/... for the three apps. `{app}` is checked by
the URL converter in config/urls.py; each view narrows it to the apps it
serves."""

from django.urls import path

from apps.crew import api

urlpatterns = [
    path("attendance/mark", api.AttendanceMarkView.as_view(), name="api-app-attendance-mark"),
    path("attendance/history", api.AttendanceHistoryView.as_view(), name="api-app-attendance-history"),
    path("attendance/team", api.TeamAttendanceView.as_view(), name="api-app-attendance-team"),
]

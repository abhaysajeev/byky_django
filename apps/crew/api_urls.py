"""/api/v1/{app}/attendance/... for the operator and manager apps. `{app}` is
checked by the URL converter in config/urls.py; the views narrow it to
`operator` and `manager`."""

from django.urls import path

from apps.crew import api

urlpatterns = [
    path("attendance/mark", api.AttendanceMarkView.as_view(), name="api-app-attendance-mark"),
    path("attendance/history", api.AttendanceHistoryView.as_view(), name="api-app-attendance-history"),
]

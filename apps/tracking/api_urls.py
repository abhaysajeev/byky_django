"""/api/v1/{app}/tracking/... for the manager app. `{app}` is checked by the
URL converter in config/urls.py; the views narrow it to `manager`."""

from django.urls import path

from apps.tracking import manager_api

urlpatterns = [
    path("tracking/alerts/summary", manager_api.SummaryView.as_view(), name="api-app-tracking-summary"),
    path("tracking/alerts/current", manager_api.CurrentView.as_view(), name="api-app-tracking-current"),
    path("tracking/alerts/history", manager_api.HistoryView.as_view(), name="api-app-tracking-history"),
]

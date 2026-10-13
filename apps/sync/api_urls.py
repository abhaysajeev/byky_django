from django.urls import path

from apps.sync import api

urlpatterns = [
    path("sync/issues/status", api.IssueStatusView.as_view(), name="api-app-sync-issue-status"),
]

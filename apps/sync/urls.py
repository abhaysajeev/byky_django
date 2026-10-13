from django.urls import path

from apps.sync import views

urlpatterns = [
    path("issues/", views.SyncIssueListView.as_view(), name="sync-issue-list"),
    path("issues/<int:pk>/", views.SyncIssueDetailView.as_view(), name="sync-issue-detail"),
    path("issues/<int:pk>/replay/", views.SyncIssueReplay.as_view(), name="sync-issue-replay"),
    path("issues/<int:pk>/dismiss/", views.SyncIssueDismiss.as_view(), name="sync-issue-dismiss"),
]

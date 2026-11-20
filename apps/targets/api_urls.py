"""/api/v1/{app}/targets... -- operator (its station) and manager (read only)."""

from django.urls import path

from apps.targets import api

urlpatterns = [
    path("targets", api.OperatorTargetsView.as_view(), name="api-app-targets"),
    path("targets/states", api.ManagerTargetStatesView.as_view(), name="api-app-targets-states"),
    path("targets/branches", api.ManagerTargetBranchesView.as_view(), name="api-app-targets-branches"),
    path("targets/branch", api.ManagerTargetBranchView.as_view(), name="api-app-targets-branch"),
]

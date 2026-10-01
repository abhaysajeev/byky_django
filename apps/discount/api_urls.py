"""/api/v1/{app}/card-discounts... for the operator app. `{app}` is checked by
the URL converter in config/urls.py; the views narrow it to `operator`."""

from django.urls import path

from apps.discount import api

urlpatterns = [
    path("card-discounts", api.CardDiscountsView.as_view(), name="api-app-card-discounts"),
    path("card-discounts/usage", api.CardUsageView.as_view(), name="api-app-card-usage"),
    path("card-discounts/approval", api.ApprovalRequestView.as_view(), name="api-app-card-approval"),
    path("card-discounts/approval/status", api.ApprovalStatusView.as_view(), name="api-app-card-approval-status"),
]

"""/api/v1/{app}/branch and /api/v1/{app}/payment-modes for the operator app.
`{app}` is checked by the URL converter in config/urls.py; the views narrow
it to `operator`."""

from django.urls import path

from apps.company import api

urlpatterns = [
    path("branch", api.BranchDetailsView.as_view(), name="api-app-branch"),
    path("payment-modes", api.PaymentModesView.as_view(), name="api-app-payment-modes"),
]

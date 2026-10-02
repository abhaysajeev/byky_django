"""/api/v1/{app}/customers/... and /api/v1/{app}/orders for the operator app.
`{app}` is checked by the URL converter in config/urls.py; the views narrow it
to `operator`."""

from django.urls import path

from apps.rental import api

urlpatterns = [
    path("customers/lookup", api.CustomerLookupView.as_view(), name="api-app-customer-lookup"),
    path("customers/create", api.CustomerCreateView.as_view(), name="api-app-customer-create"),
    path("orders", api.OrderCreateView.as_view(), name="api-app-order-create"),
    path("orders/detail", api.OrderDetailView.as_view(), name="api-app-order-detail"),
    path("orders/return", api.OrderReturnView.as_view(), name="api-app-order-return"),
    path("orders/settle", api.OrderSettleView.as_view(), name="api-app-order-settle"),
]

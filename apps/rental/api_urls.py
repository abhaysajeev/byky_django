"""/api/v1/{app}/customers/... and /api/v1/{app}/orders for the operator app,
and /api/v1/{app}/requests/... for the manager app. `{app}` is checked by the
URL converter in config/urls.py; the views narrow it to `operator` or
`manager`."""

from django.urls import path

from apps.rental import api

urlpatterns = [
    path("customers/lookup", api.CustomerLookupView.as_view(), name="api-app-customer-lookup"),
    path("customers/create", api.CustomerCreateView.as_view(), name="api-app-customer-create"),
    path("orders", api.OrderCreateView.as_view(), name="api-app-order-create"),
    path("orders/detail", api.OrderDetailView.as_view(), name="api-app-order-detail"),
    path("orders/return", api.OrderReturnView.as_view(), name="api-app-order-return"),
    path("orders/settle", api.OrderSettleView.as_view(), name="api-app-order-settle"),
    path("orders/add", api.OrderAddView.as_view(), name="api-app-order-add"),
    path("orders/replace", api.OrderReplaceView.as_view(), name="api-app-order-replace"),
    path("orders/remove", api.OrderRemoveView.as_view(), name="api-app-order-remove"),
    path("orders/payments", api.OrderPaymentsView.as_view(), name="api-app-order-payments"),
    path("orders/credit-notes", api.CreditNoteRequestView.as_view(), name="api-app-credit-note-request"),
    path("orders/credit-notes/cancel", api.CreditNoteCancelView.as_view(), name="api-app-credit-note-cancel"),
    path("orders/credit-notes/status", api.CreditNoteStatusView.as_view(), name="api-app-credit-note-status"),
    path("orders/requests", api.OrderRequestView.as_view(), name="api-app-order-request"),
    path("orders/requests/withdraw", api.OrderRequestWithdrawView.as_view(), name="api-app-order-request-withdraw"),
    path("orders/requests/status", api.OrderRequestStatusView.as_view(), name="api-app-order-request-status"),
    # Manager app.
    path("requests/list", api.ManagerRequestListView.as_view(), name="api-app-request-list"),
    path("requests/approve", api.ManagerRequestApproveView.as_view(), name="api-app-request-approve"),
    path("requests/reject", api.ManagerRequestRejectView.as_view(), name="api-app-request-reject"),
    path("requests/revoke", api.ManagerRequestRevokeView.as_view(), name="api-app-request-revoke"),
]

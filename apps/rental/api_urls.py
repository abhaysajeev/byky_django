"""/api/v1/{app}/customers/... and /api/v1/{app}/orders for the operator app,
and /api/v1/{app}/requests/... for the manager app. `{app}` is checked by the
URL converter in config/urls.py; the views narrow it to `operator` or
`manager`."""

from django.urls import path

from apps.rental import api, manager_api, test_ride_api

urlpatterns = [
    path("customers/lookup", api.CustomerLookupView.as_view(), name="api-app-customer-lookup"),
    path("customers/create", api.CustomerCreateView.as_view(), name="api-app-customer-create"),
    path("customers/blocked", api.CustomerBlockedView.as_view(), name="api-app-customer-blocked"),
    path("orders", api.OrderCreateView.as_view(), name="api-app-order-create"),
    path("orders/detail", api.OrderDetailView.as_view(), name="api-app-order-detail"),
    path("orders/changes", api.OrderChangesView.as_view(), name="api-app-order-changes"),
    path("test-rides", test_ride_api.TestRideStartView.as_view(), name="api-app-test-ride-start"),
    path("test-rides/end", test_ride_api.TestRideEndView.as_view(), name="api-app-test-ride-end"),
    path("test-rides/cancel", test_ride_api.TestRideCancelView.as_view(), name="api-app-test-ride-cancel"),
    path("test-rides/list", test_ride_api.TestRideListView.as_view(), name="api-app-test-ride-list"),
    path("invoices/recent", api.InvoiceRecentView.as_view(), name="api-app-invoice-recent"),
    path("invoices/detail", api.InvoiceDetailView.as_view(), name="api-app-invoice-detail"),
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
    path("orders/requests/used", api.OrderRequestUsedView.as_view(), name="api-app-order-request-used"),
    path("orders/requests/status", api.OrderRequestStatusView.as_view(), name="api-app-order-request-status"),
    path("orders/requests/list", api.OrderRequestListView.as_view(), name="api-app-order-request-list"),
    # Manager app.
    path("requests/list", api.ManagerRequestListView.as_view(), name="api-app-request-list"),
    path("requests/approve", api.ManagerRequestApproveView.as_view(), name="api-app-request-approve"),
    path("requests/reject", api.ManagerRequestRejectView.as_view(), name="api-app-request-reject"),
    path("requests/revoke", api.ManagerRequestRevokeView.as_view(), name="api-app-request-revoke"),
    path("states", manager_api.StatesView.as_view(), name="api-app-states"),
    path("branches", manager_api.BranchesView.as_view(), name="api-app-branches"),
    path("vehicle-types", manager_api.VehicleTypesView.as_view(), name="api-app-vehicle-types"),
    # Not "vehicles": that is the operator app's vehicles at its station (apps/fleet).
    path("vehicles/list", manager_api.VehicleListView.as_view(), name="api-app-vehicle-list"),
    path("reports/collections/states", manager_api.CollectionsByStateView.as_view(),
         name="api-app-collections-states"),
    path("reports/collections/branches", manager_api.CollectionsByBranchView.as_view(),
         name="api-app-collections-branches"),
    path("reports/orders", manager_api.BranchOrdersView.as_view(), name="api-app-report-orders"),
    path("requests/pending-counts", manager_api.PendingCountsView.as_view(),
         name="api-app-request-pending-counts"),
    path("credit-notes/list", manager_api.CreditNoteListView.as_view(), name="api-app-credit-note-list"),
    path("credit-notes/approve", manager_api.CreditNoteApproveView.as_view(), name="api-app-credit-note-approve"),
    path("credit-notes/reject", manager_api.CreditNoteRejectView.as_view(), name="api-app-credit-note-reject"),
    path("card-discounts/claims/list", manager_api.ClaimListView.as_view(), name="api-app-claim-list"),
    path("card-discounts/claims/approve", manager_api.ClaimApproveView.as_view(), name="api-app-claim-approve"),
    path("card-discounts/claims/reject", manager_api.ClaimRejectView.as_view(), name="api-app-claim-reject"),
]

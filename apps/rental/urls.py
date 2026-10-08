from django.urls import path

from apps.rental import views, writes

urlpatterns = [
    path("order/list/", views.OrderListView.as_view(), name="rental-order-list"),
    path("order/<uuid:pk>/", views.OrderDetailView.as_view(), name="rental-order-detail"),
    path("invoice/list/", views.InvoiceListView.as_view(), name="rental-invoice-list"),
    path("invoice/<uuid:pk>/", views.InvoiceDetailView.as_view(), name="rental-invoice-detail"),
    path("invoice/<uuid:pk>/receipt/", views.InvoiceReceiptView.as_view(), name="rental-invoice-receipt"),
    path("credit-note/list/", views.CreditNoteListView.as_view(), name="rental-credit-note-list"),
    path("credit-note/<uuid:pk>/", views.CreditNoteDetailView.as_view(), name="rental-credit-note-detail"),
    path("credit-note/<uuid:pk>/receipt/", views.CreditNoteReceiptView.as_view(), name="rental-credit-note-receipt"),
    path("credit-note/<uuid:pk>/approve/", writes.CreditNoteApprove.as_view(), name="rental-credit-note-approve"),
    path("credit-note/<uuid:pk>/reject/", writes.CreditNoteReject.as_view(), name="rental-credit-note-reject"),
    path("credit-note/issue/<uuid:invoice_pk>/", writes.CreditNoteIssue.as_view(), name="rental-credit-note-issue"),
    path("request/list/", views.RequestListView.as_view(), name="rental-request-list"),
    path("request/<uuid:pk>/", views.RequestDetailView.as_view(), name="rental-request-detail"),
    path("request/<uuid:pk>/approve/", writes.RequestApprove.as_view(), name="rental-request-approve"),
    path("request/<uuid:pk>/reject/", writes.RequestReject.as_view(), name="rental-request-reject"),
    path("request/<uuid:pk>/revoke/", writes.RequestRevoke.as_view(), name="rental-request-revoke"),
    path("customer/list/", views.CustomerListView.as_view(), name="rental-customer-list"),
    path("customer/save/", writes.CustomerSave.as_view(), name="rental-customer-save"),
    path("customer/<int:pk>/delete/", views.CustomerDelete.as_view(), name="rental-customer-delete"),
    path("payment-mode/list/", views.PaymentModeListView.as_view(), name="rental-payment-mode-list"),
    path("payment-mode/save/", views.PaymentModeSave.as_view(), name="rental-payment-mode-save"),
    path("payment-mode/<int:pk>/delete/", views.PaymentModeDelete.as_view(), name="rental-payment-mode-delete"),
    path("privileges/", views.RentalPrivilegeView.as_view(), name="rental-privileges"),
]

from django.urls import path

from apps.rental import views, writes

urlpatterns = [
    path("customer/list/", views.CustomerListView.as_view(), name="rental-customer-list"),
    path("customer/save/", writes.CustomerSave.as_view(), name="rental-customer-save"),
    path("customer/<int:pk>/delete/", views.CustomerDelete.as_view(), name="rental-customer-delete"),
    path("payment-mode/list/", views.PaymentModeListView.as_view(), name="rental-payment-mode-list"),
    path("payment-mode/save/", views.PaymentModeSave.as_view(), name="rental-payment-mode-save"),
    path("payment-mode/<int:pk>/delete/", views.PaymentModeDelete.as_view(), name="rental-payment-mode-delete"),
    path("privileges/", views.RentalPrivilegeView.as_view(), name="rental-privileges"),
]

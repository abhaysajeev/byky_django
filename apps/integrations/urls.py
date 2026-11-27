from django.urls import path

from apps.integrations import views

urlpatterns = [
    path("erpnext/inbox/", views.InboxListView.as_view(), name="integrations-erpnext-inbox"),
    path("erpnext/inbox/<int:pk>/", views.InboxDetailView.as_view(), name="integrations-erpnext-message"),
    path("privileges/", views.ErpnextPrivilegeView.as_view(), name="integrations-privileges"),
]

from django.urls import path

from apps.fare import views

urlpatterns = [
    path("list/", views.FareListView.as_view(), name="fare-fare-list"),
    path("add/", views.FareFormView.as_view(), name="fare-fare-add"),
    path("<int:pk>/edit/", views.FareFormView.as_view(), name="fare-fare-edit"),
    path("save/", views.FareSave.as_view(), name="fare-fare-save"),
    path("check/", views.FareCheck.as_view(), name="fare-fare-check"),
    path("<int:pk>/delete/", views.FareDelete.as_view(), name="fare-fare-delete"),
    path("privileges/", views.FarePrivilegeView.as_view(), name="fare-privileges"),
]

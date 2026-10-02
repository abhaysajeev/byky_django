from django.urls import path
from django.views.generic import RedirectView

from apps.fare import views

urlpatterns = [
    path("list/", views.FareListView.as_view(), name="fare-fare-list"),
    path("add/", views.FareFormView.as_view(), name="fare-fare-add"),
    path("<int:pk>/edit/", views.FareFormView.as_view(), name="fare-fare-edit"),
    path("save/", views.FareSave.as_view(), name="fare-fare-save"),
    path("check/", views.FareCheck.as_view(), name="fare-fare-check"),
    path("<int:pk>/delete/", views.FareDelete.as_view(), name="fare-fare-delete"),
    path("privileges/", views.FarePrivilegeView.as_view(), name="fare-privileges"),
    path("package/list/", views.PackageListView.as_view(), name="fare-package-list"),
    path("package/add/", views.PackageFormView.as_view(), name="fare-package-add"),
    path("package/<int:pk>/edit/", views.PackageFormView.as_view(), name="fare-package-edit"),
    path("package/save/", views.PackageSave.as_view(), name="fare-package-save"),
    path("package/<int:pk>/delete/", views.PackageDelete.as_view(), name="fare-package-delete"),
    # Offers were renamed Packages (2 Oct 2026): the old screen addresses
    # still land on the new ones, so bookmarks keep working.
    path("offer/list/", RedirectView.as_view(pattern_name="fare-package-list", permanent=True, query_string=True)),
    path("offer/add/", RedirectView.as_view(pattern_name="fare-package-add", permanent=True)),
    path("offer/<int:pk>/edit/", RedirectView.as_view(pattern_name="fare-package-edit", permanent=True)),
]

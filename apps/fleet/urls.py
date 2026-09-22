from django.urls import path

from apps.fleet import views

urlpatterns = [
    path("brand/list/", views.BrandListView.as_view(), name="fleet-brand-list"),
    path("brand/save/", views.BrandSave.as_view(), name="fleet-brand-save"),
    path("brand/<int:pk>/delete/", views.BrandDelete.as_view(), name="fleet-brand-delete"),
]

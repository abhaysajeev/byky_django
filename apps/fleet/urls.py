from django.urls import path

from apps.fleet import views

urlpatterns = [
    path("brand/list/", views.BrandListView.as_view(), name="fleet-brand-list"),
    path("brand/save/", views.BrandSave.as_view(), name="fleet-brand-save"),
    path("brand/<int:pk>/delete/", views.BrandDelete.as_view(), name="fleet-brand-delete"),
    path("category/list/", views.CategoryListView.as_view(), name="fleet-category-list"),
    path("category/save/", views.CategorySave.as_view(), name="fleet-category-save"),
    path("category/<int:pk>/delete/", views.CategoryDelete.as_view(), name="fleet-category-delete"),
    path("vehicle-type/list/", views.VehicleTypeListView.as_view(), name="fleet-vehicle-type-list"),
    path("vehicle-type/save/", views.VehicleTypeSave.as_view(), name="fleet-vehicle-type-save"),
    path(
        "vehicle-type/<int:pk>/delete/", views.VehicleTypeDelete.as_view(),
        name="fleet-vehicle-type-delete",
    ),
    path("uom/list/", views.UOMListView.as_view(), name="fleet-uom-list"),
    path("uom/save/", views.UOMSave.as_view(), name="fleet-uom-save"),
    path("uom/<int:pk>/delete/", views.UOMDelete.as_view(), name="fleet-uom-delete"),
    path("asset-type/list/", views.AssetTypeListView.as_view(), name="fleet-asset-type-list"),
    path("asset-type/save/", views.AssetTypeSave.as_view(), name="fleet-asset-type-save"),
    path(
        "asset-type/<int:pk>/delete/", views.AssetTypeDelete.as_view(),
        name="fleet-asset-type-delete",
    ),
    path("asset/list/", views.AssetListView.as_view(), name="fleet-asset-list"),
    path("asset/save/", views.AssetSave.as_view(), name="fleet-asset-save"),
    path("asset/<int:pk>/delete/", views.AssetDelete.as_view(), name="fleet-asset-delete"),
    path("privileges/", views.FleetPrivilegeView.as_view(), name="fleet-privileges"),
]

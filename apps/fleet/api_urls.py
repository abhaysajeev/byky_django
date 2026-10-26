"""/api/v1/{app}/... for the fleet: the operator app's vehicles and assets,
service requests and image shares (both apps). `{app}` is checked by the URL
converter in config/urls.py; each view narrows it."""

from django.urls import path

from apps.fleet import api, maintenance_api

urlpatterns = [
    path("vehicles", api.VehiclesView.as_view(), name="api-app-vehicles"),
    path("assets", maintenance_api.AssetsView.as_view(), name="api-app-assets"),
    path("service-requests", maintenance_api.ServiceRequestCreateView.as_view(), name="api-app-service-requests"),
    path("service-requests/list", maintenance_api.ServiceRequestListView.as_view(),
         name="api-app-service-request-list"),
    path("service-requests/approve", maintenance_api.ServiceRequestApproveView.as_view(),
         name="api-app-service-request-approve"),
    path("service-requests/reject", maintenance_api.ServiceRequestRejectView.as_view(),
         name="api-app-service-request-reject"),
    path("image-shares", maintenance_api.ImageShareCreateView.as_view(), name="api-app-image-shares"),
    path("image-shares/list", maintenance_api.ImageShareListView.as_view(), name="api-app-image-share-list"),
]

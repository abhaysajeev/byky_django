"""/api/v1/{app}/vehicles for the operator app. `{app}` is checked by the URL
converter in config/urls.py; the view narrows it to `operator`."""

from django.urls import path

from apps.fleet import api

urlpatterns = [
    path("vehicles", api.VehiclesView.as_view(), name="api-app-vehicles"),
]

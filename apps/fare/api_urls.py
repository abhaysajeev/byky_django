"""/api/v1/{app}/fares and /packages for the operator app. `{app}` is checked by the URL
converter in config/urls.py; the view narrows it to `operator`."""

from django.urls import path

from apps.fare import api

urlpatterns = [
    path("fares", api.FaresView.as_view(), name="api-app-fares"),
    path("packages", api.PackagesView.as_view(), name="api-app-packages"),
]

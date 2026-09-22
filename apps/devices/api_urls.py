"""/api/v1/{app}/... for the phone apps. `{app}` is checked by the URL
converter in config/urls.py, so every view here receives a valid one."""

from django.urls import path

from apps.devices import api

urlpatterns = [
    path("app/update-check", api.UpdateCheckView.as_view(), name="api-app-update-check"),
    path("device/registration", api.RegistrationView.as_view(), name="api-device-registration"),
]

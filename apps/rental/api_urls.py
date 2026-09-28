"""/api/v1/{app}/customers/lookup for the operator app. `{app}` is checked by
the URL converter in config/urls.py; the view narrows it to `operator`."""

from django.urls import path

from apps.rental import api

urlpatterns = [
    path("customers/lookup", api.CustomerLookupView.as_view(), name="api-app-customer-lookup"),
]

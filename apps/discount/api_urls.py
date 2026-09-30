"""/api/v1/{app}/card-discounts... for the operator app. `{app}` is checked by
the URL converter in config/urls.py; the views narrow it to `operator`."""

from django.urls import path

from apps.discount import api

urlpatterns = [
    path("card-discounts", api.CardDiscountsView.as_view(), name="api-app-card-discounts"),
    path("card-discounts/usage", api.CardUsageView.as_view(), name="api-app-card-usage"),
]

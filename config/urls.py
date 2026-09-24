from django.urls import include, path, re_path, register_converter
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView

from core import api as core_api
from core import views as core_views

register_converter(core_api.AppConverter, "app")

urlpatterns = [
    path("healthz/", core_views.healthz, name="healthz"),
    path("company/", include("apps.company.urls")),
    path("crew/", include("apps.crew.urls")),
    path("devices/", include("apps.devices.urls")),
    path("fleet/", include("apps.fleet.urls")),
    path("fare/", include("apps.fare.urls")),
    path("api/v1/<app:app>/", include("apps.devices.api_urls")),
    path("api/v1/<app:app>/", include("apps.portal.api_urls")),
    # Must come before the catch-all below, or it swallows these too.
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    # Anything else under /api/ -- `web`, an unknown app, a typo -- answers in
    # the envelope rather than as an HTML page.
    re_path(r"^api/", core_api.not_found),
    path("", include("apps.portal.urls")),
]

# Module URLs are added here as each module is built.

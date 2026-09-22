"""/api/v1/{app}/auth/... and /api/v1/{app}/server/time. `{app}` is checked by
the URL converter in config/urls.py, so every view here receives a valid one;
LoginView itself narrows further to the channels actually built (employee,
operator). auth/... is design/03-login.md section 9.1; server/time is
design/registration/server-time-api.md."""

from django.urls import path

from apps.portal import api

urlpatterns = [
    path("auth/login", api.LoginView.as_view(), name="api-app-login"),
    path("auth/refresh", api.AppRefreshView.as_view(), name="api-app-refresh"),
    path("auth/logout", api.AppLogoutView.as_view(), name="api-app-logout"),
    path("server/time", api.ServerTimeView.as_view(), name="api-server-time"),
]

from django.urls import path

from apps.monitoring import views

urlpatterns = [
    path("requests/", views.RequestListView.as_view(), name="monitoring-request-list"),
    path("requests/<uuid:pk>/", views.RequestDetailView.as_view(), name="monitoring-request-detail"),
    path("errors/", views.ErrorListView.as_view(), name="monitoring-error-list"),
    path("errors/<uuid:pk>/", views.ErrorDetailView.as_view(), name="monitoring-error-detail"),
]

"""/api/v1/{app}/photos -- `{app}` is checked by the URL converter in
config/urls.py; each view narrows it to the apps it serves."""

from django.urls import path

from apps.photos import api

urlpatterns = [
    path("photos", api.PhotoUploadView.as_view(), name="api-app-photo-upload"),
    path("photos/<uuid:photo_id>", api.PhotoView.as_view(), name="api-app-photo"),
]

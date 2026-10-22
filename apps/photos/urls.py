from django.urls import path

from apps.photos import views

urlpatterns = [
    path("<uuid:pk>/<str:size>/", views.PhotoView.as_view(), name="photo"),
]

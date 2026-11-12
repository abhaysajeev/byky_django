from django.urls import path

from apps.targets import views

urlpatterns = [
    path("", views.TargetListView.as_view(), name="targets-list"),
    path("branch/<int:pk>/", views.BranchView.as_view(), name="targets-branch"),
    path("branch/<int:pk>/<int:year>-<int:month>/", views.MonthView.as_view(), name="targets-month"),
    path("month/<int:pk>/data/", views.MonthDataView.as_view(), name="targets-month-data"),
    path("save/", views.TargetSaveView.as_view(), name="targets-save"),
    path("<int:pk>/delete/", views.TargetDeleteView.as_view(), name="targets-delete"),
    path("privileges/", views.TargetPrivilegeView.as_view(), name="targets-privileges"),
]

from django.urls import path

from apps.targets import views

urlpatterns = [
    path("", views.OverviewView.as_view(), name="targets-overview"),
    path("daily/", views.DailyView.as_view(), name="targets-daily"),
    path("manage/", views.ManageView.as_view(), name="targets-manage"),
    path("station/<int:pk>/<int:year>-<int:month>/", views.StationView.as_view(), name="targets-station"),
    path("month/<int:pk>/data/", views.MonthDataView.as_view(), name="targets-month-data"),
    path("save/", views.TargetSaveView.as_view(), name="targets-save"),
    path("<int:pk>/delete/", views.TargetDeleteView.as_view(), name="targets-delete"),
    path("privileges/", views.TargetPrivilegeView.as_view(), name="targets-privileges"),
]

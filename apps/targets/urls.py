from django.urls import path

from apps.targets import views

urlpatterns = [
    path("", views.OverviewView.as_view(), name="targets-overview"),
    path("daily/", views.DailyView.as_view(), name="targets-daily"),
    path("profiles/", views.ProfilesView.as_view(), name="targets-profiles"),
    path("profiles/add/", views.ProfileFormView.as_view(), name="targets-profile-add"),
    path("profiles/<int:pk>/", views.ProfileFormView.as_view(), name="targets-profile-edit"),
    path("profiles/save/", views.ProfileSaveView.as_view(), name="targets-profile-save"),
    path("profiles/<int:pk>/delete/", views.ProfileDeleteView.as_view(), name="targets-profile-delete"),
    path("mapping/", views.MappingView.as_view(), name="targets-mapping"),
    path("mapping/save/", views.MappingSaveView.as_view(), name="targets-mapping-save"),
    path("manage/", views.ManageView.as_view(), name="targets-manage"),
    path("station/<int:pk>/", views.StationView.as_view(), name="targets-station"),
    path("station/<int:pk>/<int:year>-<int:month>/", views.StationMonthView.as_view(), name="targets-station-month"),
    path("target/<int:pk>/", views.TargetView.as_view(), name="targets-target"),
    path("month/<int:pk>/data/", views.MonthDataView.as_view(), name="targets-month-data"),
    path("save/", views.TargetSaveView.as_view(), name="targets-save"),
    path("<int:pk>/delete/", views.TargetDeleteView.as_view(), name="targets-delete"),
    path("privileges/", views.TargetPrivilegeView.as_view(), name="targets-privileges"),
]

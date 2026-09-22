from django.urls import path

from apps.company import views

urlpatterns = [
    # Screens
    path("company/list/", views.CompanyListView.as_view(), name="company-company-list"),
    path("country-state/list/", views.CountryStateListView.as_view(), name="company-country-state-list"),
    path("location/list/", views.LocationListView.as_view(), name="company-location-list"),
    path("department/list/", views.DepartmentListView.as_view(), name="company-department-list"),
    path("branch/list/", views.BranchListView.as_view(), name="company-branch-list"),
    path("branch-working-time/edit/", views.BranchWorkingTimeView.as_view(), name="company-branch-working-time-edit"),
    path("privileges/", views.CompanyPrivilegeView.as_view(), name="company-privileges"),

    # Writes
    path("company/save/", views.CompanySave.as_view(), name="company-company-save"),
    path("company/<int:pk>/delete/", views.CompanyDelete.as_view(), name="company-company-delete"),
    path("country/save/", views.CountrySave.as_view(), name="company-country-save"),
    path("country/<int:pk>/delete/", views.CountryDelete.as_view(), name="company-country-delete"),
    path("state/save/", views.StateSave.as_view(), name="company-state-save"),
    path("state/<int:pk>/delete/", views.StateDelete.as_view(), name="company-state-delete"),
    path("location/save/", views.LocationSave.as_view(), name="company-location-save"),
    path("location/<int:pk>/delete/", views.LocationDelete.as_view(), name="company-location-delete"),
    path("department/save/", views.DepartmentSave.as_view(), name="company-department-save"),
    path("department/<int:pk>/delete/", views.DepartmentDelete.as_view(), name="company-department-delete"),
    path("branch/save/", views.BranchSave.as_view(), name="company-branch-save"),
    path("branch/<int:pk>/delete/", views.BranchDelete.as_view(), name="company-branch-delete"),
    path("branch-working-time/save/", views.BranchWorkingTimeSave.as_view(), name="company-branch-working-time-save"),
    path("branch-working-time/assign/", views.BranchWorkingTimeAssign.as_view(), name="company-branch-working-time-assign"),
    path("branch-working-time/schedule/<int:branch_id>/", views.BranchWorkingTimeSchedule.as_view(), name="company-branch-working-time-schedule"),
]

from django.urls import path

from apps.incentives import views

urlpatterns = [
    path("", views.CalculateView.as_view(), name="incentives-calculate"),
    path("calculate/", views.CalculatePost.as_view(), name="incentives-calculate-post"),
    path("run/<int:pk>/", views.RunView.as_view(), name="incentives-run"),
    path("run/<int:pk>/approve/", views.ApprovePost.as_view(), name="incentives-approve"),
    path("run/<int:pk>/export/", views.RunExport.as_view(), name="incentives-run-export"),
    path("employees/", views.EmployeesView.as_view(), name="incentives-employees"),
    path("employees/export/", views.EmployeesExport.as_view(), name="incentives-employees-export"),
    path("schemes/", views.SchemeListView.as_view(), name="incentives-schemes"),
    path("schemes/add/", views.SchemeFormView.as_view(), name="incentives-scheme-add"),
    path("schemes/<int:pk>/edit/", views.SchemeFormView.as_view(), name="incentives-scheme-edit"),
    path("schemes/save/", views.SchemeSave.as_view(), name="incentives-scheme-save"),
    path("schemes/<int:pk>/copy/", views.SchemeCopy.as_view(), name="incentives-scheme-copy"),
    path("schemes/<int:pk>/delete/", views.SchemeDelete.as_view(), name="incentives-scheme-delete"),
    path("schemes/check/", views.FormulaCheck.as_view(), name="incentives-formula-check"),
    path("schemes/try/", views.SchemeTry.as_view(), name="incentives-scheme-try"),
    path("privileges/", views.IncentivePrivilegeView.as_view(), name="incentives-privileges"),
]

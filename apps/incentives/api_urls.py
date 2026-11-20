"""/api/v1/{app}/incentives... -- manager (every station) and employee (own)."""

from django.urls import path

from apps.incentives import api

urlpatterns = [
    path("incentives/branches", api.ManagerIncentiveBranchesView.as_view(), name="api-app-incentives-branches"),
    path("incentives/employees", api.ManagerIncentiveEmployeesView.as_view(), name="api-app-incentives-employees"),
    path("incentives/run", api.ManagerIncentiveRunView.as_view(), name="api-app-incentives-run"),
    path("incentives/year", api.EmployeeIncentiveYearView.as_view(), name="api-app-incentives-year"),
    path("incentives/month", api.EmployeeIncentiveMonthView.as_view(), name="api-app-incentives-month"),
]

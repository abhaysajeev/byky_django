"""/reports/<slug>/ -- the page; /data/ its JSON; /export/ its Excel."""

from django.urls import path

from apps.reports import views
from apps.reports.defs import REPORTS

urlpatterns = []
for _report in REPORTS:
    urlpatterns += [
        path(f"{_report.slug}/", views.ReportView.as_view(report=_report), name=f"reports-{_report.slug}"),
        path(f"{_report.slug}/data/", views.ReportDataView.as_view(report=_report),
             name=f"reports-{_report.slug}-data"),
        path(f"{_report.slug}/export/", views.ReportExportView.as_view(report=_report),
             name=f"reports-{_report.slug}-export"),
    ]

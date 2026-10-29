"""The reports, in the order the sidebar lists them."""

from apps.reports.defs.consolidated import Consolidated
from apps.reports.defs.rent_details import RentDetails
from apps.reports.defs.rent_summary import RentSummary

REPORTS = [RentSummary(), Consolidated(), RentDetails()]
BY_SLUG = {report.slug: report for report in REPORTS}

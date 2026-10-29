"""The reports, in the order the sidebar lists them."""

from apps.reports.defs.card_discount import CardDiscountDetails
from apps.reports.defs.consolidated import Consolidated
from apps.reports.defs.credit_note import CreditNoteDetails
from apps.reports.defs.customer_card import CustomerWiseCard
from apps.reports.defs.customer_rent import CustomerWiseRent
from apps.reports.defs.fleet import AssetDetails, VehicleDetails
from apps.reports.defs.rent_details import RentDetails
from apps.reports.defs.rent_summary import RentSummary
from apps.reports.defs.replacement import ReplacementDetails

REPORTS = [
    RentSummary(), Consolidated(), RentDetails(), ReplacementDetails(), CardDiscountDetails(),
    CreditNoteDetails(), CustomerWiseRent(), CustomerWiseCard(), VehicleDetails(), AssetDetails(),
]
BY_SLUG = {report.slug: report for report in REPORTS}

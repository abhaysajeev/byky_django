"""Customer Wise Discount Card -- the client's sheet "Customer Wise Discount Card
Details Report".

One row per customer x card (type, grade, number) seen on an approved or
redeemed card claim. We keep no register of a customer's cards, so Created On,
State and Created From Branch are that card's **first** claim -- and the date
range picks cards first used in it (owner, 29 Oct 2026).
"""

from apps.discount.models import ClaimStatus
from apps.discount.scoping import claims_for
from apps.reports.base import COUNT, DATETIME, Column, Kpi, Report, Row, local, paged

USED = (ClaimStatus.APPROVED, ClaimStatus.REDEEMED)


class CustomerWiseCard(Report):
    slug = "customer-wise-card"
    title = "Customer Wise Discount Card"
    sheet_title = "Customer Wise Discount Card Details Report"
    page_code = "reports.customer_wise_card"
    filters = ("state", "branch", "customer", "phone", "card_no", "card_type", "card_grade")
    unit = ("card", "cards")
    empty = "No discount card was first used in this range."
    description = "Each customer's discount cards, from the first bill they were used on."

    def build(self, ctx, page=None, per_page=None):
        f = ctx.f
        claims = claims_for(ctx.user).filter(status__in=USED)
        if f.get("customer"):
            claims = claims.filter(customer_name__icontains=f.get("customer"))
        if f.get("phone"):
            digits = "".join(ch for ch in f.get("phone") if ch.isdigit())
            claims = claims.filter(mobile_full__icontains=digits) if digits else claims.none()
        if f.get("card_no"):
            claims = claims.filter(card_number__icontains=f.get("card_no"))
        if f.get("card_type"):
            claims = claims.filter(card_type_id=f.get("card_type"))
        if f.get("card_grade"):
            claims = claims.filter(card_grade_id=f.get("card_grade"))
        claims = (claims.select_related("customer", "card_type", "card_grade", "branch__location__state")
                  .order_by("requested_at", "pk"))

        first = {}
        for claim in claims:                      # oldest first: the first claim of each card wins
            first.setdefault((claim.customer_id, claim.card_type_id, claim.card_grade_id,
                              claim.card_number.strip().upper()), claim)
        picked = []
        for claim in first.values():
            if not (f.since <= claim.requested_at < f.until):
                continue
            branch = claim.branch
            if f.get("state") and (branch is None or branch.location.state_id != f.get("state")):
                continue
            if f.get("branch") and claim.branch_id != f.get("branch"):
                continue
            picked.append(claim)

        columns = [
            Column("phone", "Phone Number", width=16),
            Column("customer", "Customer Name", width=22),
            Column("email", "Email ID", width=24),
            Column("created", "Created On", DATETIME, width=21),
            Column("state", "State", width=14),
            Column("branch", "Created From Branch", width=24),
            Column("card_type", "Card Type", width=16),
            Column("card_grade", "Card Grade", width=24),
            Column("card_no", "Card Number", width=20),
        ]
        rows = [Row({
            "phone": c.mobile_full, "customer": c.customer_name or c.customer.full_name,
            "email": c.customer.email or None, "created": local(c.requested_at, ctx.zone),
            "state": c.branch.location.state.name if c.branch else None,
            "branch": c.branch.name if c.branch else None, "card_type": c.card_type.name,
            "card_grade": c.card_grade.name, "card_no": c.card_number or None,
        }) for c in picked]
        return paged(rows, page, per_page, columns=columns, total=None, count=len(rows),
                     kpis=[Kpi("Cards", len(rows), COUNT),
                           Kpi("Customers", len({c.customer_id for c in picked}), COUNT),
                           Kpi("Card types", len({c.card_type_id for c in picked}), COUNT)])

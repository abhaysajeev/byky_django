"""Discount Card screens.

Same shape as the other modules: a shared screen base with the lists its
drawers and filters read, and the generic save/delete from apps.company.writes
for the two drawer masters. Card Discount has its own page (a day grid does
not fit a drawer); the approval and history screens read CardDiscountClaim.
"""

import csv
import datetime

from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, StreamingHttpResponse
from django.urls import reverse
from django.utils import timezone

from apps.company import writes as company_writes
from apps.company.models import UAE_WEEK, WeekDay
from apps.company.scoping import branches_for, companies_for
from apps.discount import drawers, forms, scoping, services
from apps.discount.models import CardDiscount, CardGrade, CardType, ClaimStatus, UsageType
from apps.portal.permissions import PagePermissionMixin
from apps.portal.screens import PrivilegeScreenView
from apps.portal.services import has_permission
from apps.rental.models import OrderItemStatus
from core.ordering import recent_first
from core.timezones import zone_for
from theme import drawers as theme_drawers
from theme.views import ThemedTemplateView

ACTIONS = ("create", "read", "update", "delete", "approve", "print")


class DiscountScreenView(PagePermissionMixin, ThemedTemplateView):
    drawer_specs = drawers.SPECS

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context.update({
            "companies_list": list(companies_for(user).filter(is_active=True).values("id", "name")),
            "card_types_list": list(
                scoping.card_types_for(user).filter(is_active=True).values("id", "name", "company_id")
            ),
            "perm": {action: has_permission(user, self.page_code, action) for action in ACTIONS},
        })
        return context

    def render_to_response(self, context, **response_kwargs):
        specs = theme_drawers.all_for_user(self.drawer_specs, self.request.user.sees_every_company)
        context.update(theme_drawers.resolve_all(specs, context))
        return super().render_to_response(context, **response_kwargs)


# -- Card Type ----------------------------------------------------------------


class CardTypeListView(DiscountScreenView):
    template_name = "discount/card_type_list.html"
    page_code = "discount.card_type"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        types = recent_first(scoping.card_types_for(self.request.user)).annotate(grade_count=Count("grades"))
        context.update({
            "rows": [
                {
                    "code": t.code, "name": t.name, "grades": t.grade_count, "active": t.is_active,
                    "pk": t.pk, "json_id": f"scr-record-card-type-{i}",
                    "fields_json": {"pk": t.pk, "company": t.company_id, "code": t.code, "name": t.name,
                                    "is_active": t.is_active},
                }
                for i, t in enumerate(types)
            ],
            "save_url": reverse("discount-card-type-save"),
            "delete_url": reverse("discount-card-type-delete", args=[0]),
        })
        return context


class CardTypeSave(company_writes.EntitySaveView):
    model = CardType
    form_class = forms.CardTypeForm
    page_code = "discount.card_type"
    noun = "Card type"


class CardTypeDelete(company_writes.EntityDeleteView):
    model = CardType
    page_code = "discount.card_type"
    noun = "Card type"


# -- Card Grade ---------------------------------------------------------------


class CardGradeListView(DiscountScreenView):
    template_name = "discount/card_grade_list.html"
    page_code = "discount.card_grade"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        grades = recent_first(scoping.card_grades_for(self.request.user)).select_related("card_type")
        context.update({
            "rows": [
                {
                    "code": g.code, "name": g.name, "card_type": g.card_type.name, "active": g.is_active,
                    "pk": g.pk, "json_id": f"scr-record-card-grade-{i}",
                    "fields_json": {"pk": g.pk, "company": g.company_id, "card_type": g.card_type_id,
                                    "code": g.code, "name": g.name, "is_active": g.is_active},
                }
                for i, g in enumerate(grades)
            ],
            "type_names": sorted({t["name"] for t in context["card_types_list"]}),
            "save_url": reverse("discount-card-grade-save"),
            "delete_url": reverse("discount-card-grade-delete", args=[0]),
        })
        return context


class CardGradeSave(company_writes.EntitySaveView):
    model = CardGrade
    form_class = forms.CardGradeForm
    page_code = "discount.card_grade"
    noun = "Card grade"


class CardGradeDelete(company_writes.EntityDeleteView):
    model = CardGrade
    page_code = "discount.card_grade"
    noun = "Card grade"


# -- Card Discount ------------------------------------------------------------


def _days_label(discount):
    days = list(discount.days.all())
    if len(days) == 7 and len({d.discount_percent for d in days}) == 1:
        return f"All days · {_pct(days[0].discount_percent)}"
    by_day = {d.weekday: d for d in days}
    return " · ".join(f"{WeekDay(day).label[:3]} {_pct(by_day[day].discount_percent)}"
                      for day in UAE_WEEK if day in by_day)


def _pct(value):
    return f"{value.normalize():f}%"


class CardDiscountListView(DiscountScreenView):
    template_name = "discount/card_discount_list.html"
    page_code = "discount.card_discount"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        discounts = (recent_first(scoping.card_discounts_for(self.request.user))
                     .select_related("card_grade__card_type").prefetch_related("days"))
        context.update({
            "rows": [
                {
                    "pk": d.pk, "card_type": d.card_grade.card_type.name, "grade": d.card_grade.name,
                    "valid_from": d.valid_from, "valid_to": d.valid_to, "days": _days_label(d),
                    "promotion": d.promotion_label, "usage": d.usage_label, "active": d.is_active,
                    "edit_url": reverse("discount-card-discount-edit", args=[d.pk]),
                }
                for d in discounts
            ],
            "type_names": sorted({t["name"] for t in context["card_types_list"]}),
            "add_url": reverse("discount-card-discount-add"),
            "delete_url": reverse("discount-card-discount-delete", args=[0]),
        })
        return context


class CardDiscountFormView(DiscountScreenView):
    """Add (no pk) or edit (pk) -- one page, posted whole to writes.CardDiscountSave."""

    template_name = "discount/card_discount_form.html"
    page_code = "discount.card_discount"

    def dispatch(self, request, *args, **kwargs):
        self.required_action = "read" if kwargs.get("pk") else "create"
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        discount = None
        if self.kwargs.get("pk"):
            discount = (scoping.card_discounts_for(user).select_related("card_grade__card_type")
                        .prefetch_related("days").filter(pk=self.kwargs["pk"]).first())
            if discount is None:
                raise Http404("No such discount.")

        linked_grade = discount.card_grade_id if discount else None
        grades = (scoping.card_grades_for(user).select_related("card_type")
                  .filter(Q(is_active=True) | Q(pk=linked_grade)).order_by("card_type__name", "name"))
        types = {g.card_type_id: g.card_type for g in grades}
        types.update({t.pk: t for t in scoping.card_types_for(user).filter(is_active=True)})

        context.update({
            "discount": discount,
            "is_edit": discount is not None,
            "can_save": context["perm"]["update" if discount else "create"],
            "initial": _discount_initial(discount, user),
            "types": [{"id": t.pk, "name": t.name, "company": t.company_id}
                      for t in sorted(types.values(), key=lambda t: t.name)],
            "grades": [{"id": g.pk, "name": g.name, "type": g.card_type_id} for g in grades],
            "days": [{"value": day.value, "label": day.label} for day in UAE_WEEK],
            "promotions": [{"value": v, "label": label} for v, label, _, _ in services.PROMOTIONS],
            "usages": UsageType.choices,
            "companies": list(companies_for(user).filter(is_active=True).order_by("name").values("id", "name")),
            "sees_every_company": user.sees_every_company,
            "save_url": reverse("discount-card-discount-save"),
            "list_url": reverse("discount-card-discount-list"),
        })
        return context


def _discount_initial(discount, user):
    if discount is None:
        return {"pk": None, "company": None if user.sees_every_company else user.company_id,
                "card_type": None, "card_grade": None, "valid_from": "", "valid_to": "",
                "all_days": True, "all_days_percent": "", "days": [], "promotion": "auto_base",
                "usage_type": UsageType.ONE_TIME, "usage_limit": None, "is_active": True}
    days = list(discount.days.all())
    all_days = len(days) == 7 and len({d.discount_percent for d in days}) == 1
    return {
        "pk": discount.pk, "company": discount.company_id,
        "card_type": discount.card_grade.card_type_id, "card_grade": discount.card_grade_id,
        "valid_from": discount.valid_from.isoformat(), "valid_to": discount.valid_to.isoformat(),
        "all_days": all_days, "all_days_percent": str(days[0].discount_percent) if all_days else "",
        "days": [] if all_days else [{"weekday": d.weekday, "percent": str(d.discount_percent)} for d in days],
        "promotion": services.promotion_of(discount),
        "usage_type": discount.usage_type, "usage_limit": discount.usage_limit,
        "is_active": discount.is_active,
    }


class CardDiscountDelete(company_writes.EntityDeleteView):
    model = CardDiscount
    page_code = "discount.card_discount"
    noun = "Card discount"


# -- Approval -----------------------------------------------------------------

APPROVAL_TABS = [
    (ClaimStatus.PENDING, "Pending"),
    (ClaimStatus.APPROVED, "Approved"),
    (ClaimStatus.REJECTED, "Rejected"),
    (ClaimStatus.CANCELLED, "Cancelled"),
]


def _order_no(order):
    return order.order_no or str(order.pk)


def _claim_row(claim, zone):
    requested = claim.requested_at.astimezone(zone)
    return {
        "pk": claim.pk, "customer": claim.customer_name, "phone": claim.mobile_full,
        "card_type": claim.card_type.name, "grade": claim.card_grade.name,
        "card_number": claim.card_number, "percent": _pct(claim.discount_percent),
        "fare_basis": claim.get_fare_basis_display(), "branch": claim.branch.name if claim.branch_id else "",
        "requested_by": claim.requested_by.display_name if claim.requested_by_id else "",
        "requested_at": requested, "status": claim.status, "status_label": claim.get_status_display(),
        "order_no": _order_no(claim.order),
        "order_start": claim.order.start_time.astimezone(zone),
        "order_vehicles": claim.order.items.exclude(status=OrderItemStatus.REPLACED).count(),
        "order_total": claim.order.subtotal, "order_net": claim.order.net_amount,       # blank until settled
        "order_status": claim.order.get_status_display(),
        "bill_amount": claim.bill_amount, "discount_amount": claim.discount_amount,
        "net_amount": claim.net_amount,
        "photo": claim.card_photo, "photo_is_image": claim.card_photo.startswith(("http://", "https://")),
        "decided_at": claim.decided_at.astimezone(zone) if claim.decided_at else None,
        "decided_by": claim.decided_by.display_name if claim.decided_by_id else "",
        "remarks": claim.remarks,
        "redeemed_at": claim.redeemed_at.astimezone(zone) if claim.redeemed_at else None,
        "detail_url": reverse("discount-approval-detail", args=[claim.pk]),
    }


class ApprovalListView(DiscountScreenView):
    template_name = "discount/approval_list.html"
    page_code = "discount.approval"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        zone = zone_for(getattr(self.request.user, "company", None))
        claims = (scoping.claims_for(self.request.user).filter(requires_approval=True)
                  .select_related("card_type", "card_grade", "branch", "requested_by", "decided_by", "order"))
        tab = self.request.GET.get("tab", ClaimStatus.PENDING)
        if tab not in dict(APPROVAL_TABS):
            tab = ClaimStatus.PENDING
        counts = dict(claims.values_list("status").annotate(n=Count("pk")))
        context.update({
            "tab": tab,
            "tabs": [{"value": v, "label": label, "count": counts.get(v, 0)} for v, label in APPROVAL_TABS],
            "rows": [_claim_row(c, zone) for c in claims.filter(status=tab).order_by("-requested_at")[:500]],
            "type_names": sorted({t["name"] for t in context["card_types_list"]}),
        })
        return context


class ApprovalDetailView(DiscountScreenView):
    template_name = "discount/approval_detail.html"
    page_code = "discount.approval"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        zone = zone_for(getattr(user, "company", None))
        related = ("card_type", "card_grade", "branch", "requested_by", "decided_by", "order")
        claim = scoping.claims_for(user).select_related(*related).filter(pk=self.kwargs["pk"]).first()
        if claim is None:
            raise Http404("No such request.")
        history = (scoping.claims_for(user).select_related(*related)
                   .filter(customer_id=claim.customer_id).exclude(pk=claim.pk).order_by("-requested_at")[:100])
        context.update({
            "claim": _claim_row(claim, zone),
            "history": [_claim_row(c, zone) for c in history],
            "redeemed_count": scoping.claims_for(user).filter(
                customer_id=claim.customer_id, card_discount_id=claim.card_discount_id,
                status=ClaimStatus.REDEEMED).count(),
            "usage": claim.card_discount.usage_label,
            "list_url": reverse("discount-approval-list"),
            "approve_url": reverse("discount-approval-approve", args=[claim.pk]),
            "reject_url": reverse("discount-approval-reject", args=[claim.pk]),
        })
        return context


# -- Redemption History --------------------------------------------------------

REDEMPTION_PER_PAGE = 50


def _date_param(value):
    try:
        return datetime.date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _redemptions(request):
    """Redeemed claims, narrowed by the query string -- one function for the
    list and its export, so an export is exactly what the screen shows."""
    params = request.GET
    zone = zone_for(getattr(request.user, "company", None))
    claims = (scoping.claims_for(request.user).filter(status=ClaimStatus.REDEEMED)
              .select_related("card_type", "card_grade", "branch", "requested_by", "decided_by", "order"))
    text = params.get("q", "").strip()
    if text:
        claims = claims.filter(Q(customer_name__icontains=text) | Q(mobile_full__icontains=text)
                               | Q(card_number__icontains=text) | Q(order__order_no__icontains=text))
    if params.get("type", "").isdigit():
        claims = claims.filter(card_type_id=int(params["type"]))
    if params.get("branch", "").isdigit():
        claims = claims.filter(branch_id=int(params["branch"]))
    start, end = _date_param(params.get("from")), _date_param(params.get("to"))
    if start:
        claims = claims.filter(redeemed_at__gte=datetime.datetime.combine(start, datetime.time.min, tzinfo=zone))
    if end:
        claims = claims.filter(redeemed_at__lt=datetime.datetime.combine(
            end + datetime.timedelta(days=1), datetime.time.min, tzinfo=zone))
    return claims.order_by("-redeemed_at"), zone


class RedemptionListView(DiscountScreenView):
    template_name = "discount/redemption_list.html"
    page_code = "discount.redemption"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        claims, zone = _redemptions(self.request)
        page = Paginator(claims, REDEMPTION_PER_PAGE).get_page(self.request.GET.get("page"))
        query = self.request.GET.copy()
        query.pop("page", None)
        context.update({
            "rows": [_claim_row(c, zone) for c in page.object_list],
            "page": page,
            "query": query.urlencode(),
            "params": self.request.GET,
            "filtered": any(self.request.GET.get(k) for k in ("q", "type", "branch", "from", "to")),
            "branches": list(branches_for(self.request.user).filter(is_active=True).order_by("name")
                             .values("id", "name")),
            "export_url": reverse("discount-redemption-export"),
        })
        return context


class _Echo:
    def write(self, value):
        return value


REDEMPTION_COLUMNS = ("Redeemed", "Customer", "Phone", "Card Type", "Card Grade", "Card No", "Discount %",
                      "Fare", "Order", "Bill Amount", "Branch", "Requested By", "Approved By")


class RedemptionExport(DiscountScreenView):
    page_code = "discount.redemption"
    required_action = "print"

    def get(self, request, *args, **kwargs):
        claims, zone = _redemptions(request)
        writer = csv.writer(_Echo())

        def rows():
            yield "﻿"
            yield writer.writerow(REDEMPTION_COLUMNS)
            for c in claims.iterator(chunk_size=1000):
                yield writer.writerow([
                    c.redeemed_at.astimezone(zone).strftime("%Y-%m-%d %H:%M"), c.customer_name, c.mobile_full,
                    c.card_type.name, c.card_grade.name, c.card_number, c.discount_percent,
                    c.get_fare_basis_display(), _order_no(c.order), c.bill_amount if c.bill_amount is not None else "",
                    c.branch.name if c.branch_id else "",
                    c.requested_by.display_name if c.requested_by_id else "",
                    c.decided_by.display_name if c.decided_by_id else "",
                ])

        response = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="card-redemptions-{timezone.localdate()}.csv"'
        return response


class DiscountPrivilegeView(PrivilegeScreenView):
    page_code = "discount.privileges"
    module_code = "discount"
    module_label = "Discount Card"

"""Discount Card writes that are not a plain drawer save: the Card Discount
page's post, and Approve / Reject on a claim."""

from django.http import JsonResponse
from django.shortcuts import get_object_or_404

from apps.company.writes import WriteView, _payload
from apps.discount import services
from apps.discount.models import CardDiscount, CardDiscountClaim
from apps.discount.scoping import claims_for


class CardDiscountSave(WriteView):
    model = CardDiscount
    page_code = "discount.card_discount"

    def post(self, request, *args, **kwargs):
        data = _payload(request)
        if not self.may("update" if data.get("pk") else "create"):
            return self.refused()
        try:
            discount = services.save_card_discount(request.user, data)
        except services.NotFound:
            return JsonResponse({"ok": False, "code": "not_found",
                                 "errors": [{"field": "", "message": "This discount no longer exists."}]}, status=404)
        except services.Invalid as invalid:
            return JsonResponse({"ok": False, "code": "invalid", "errors": invalid.errors}, status=400)
        return JsonResponse({"ok": True, "pk": discount.pk, "message": "Card discount saved."})


class _ClaimDecision(WriteView):
    model = CardDiscountClaim
    page_code = "discount.approval"
    approve = True
    done = ""

    def rows(self):
        return claims_for(self.request.user)

    def post(self, request, pk, *args, **kwargs):
        if not self.may("approve"):
            return self.refused()
        claim = get_object_or_404(self.rows(), pk=pk)
        remarks = str(_payload(request).get("remarks") or "")
        try:
            services.decide_claim(request.user, claim, self.approve, remarks)
        except services.NotPending:
            return JsonResponse({"ok": False, "code": "invalid",
                                 "errors": [{"field": "", "message": "This request is already decided."}]},
                                status=400)
        return JsonResponse({"ok": True, "message": self.done})


class ClaimApprove(_ClaimDecision):
    approve = True
    done = "Request approved."


class ClaimReject(_ClaimDecision):
    approve = False
    done = "Request rejected."

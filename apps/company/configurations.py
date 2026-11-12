"""Configurations (owner, 3 Nov 2026 -- client change request 1): a company's
report day start and its money settings per country.

One page, one save: the report day start and every country's money row are
posted together and replaced in one transaction. Company admins see their own
company; system users choose one (?company=).
"""

import datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone

from apps.company import dial_codes, scoping
from apps.company.models import (
    DECIMAL_PLACES,
    ROUND_OFF_STEPS,
    CompanyConfig,
    CompanyDialCode,
    CompanyMoneySetting,
    DialCode,
    RoundOffMode,
)
from apps.company.writes import WriteView, _payload
from core.money import fmt

PAGE = "company.configurations"
PLACES = [p for p, _ in DECIMAL_PLACES]


def company_for(request, value=None):
    """The company this page works on: the user's own, or for a system user
    the one asked for (else the first)."""
    rows = scoping.companies_for(request.user).order_by("name")
    if value not in (None, "") and str(value).isdigit():
        return rows.filter(pk=int(value)).first()
    own = getattr(request.user, "company_id", None)
    return rows.filter(pk=own).first() if own else rows.first()


def money_rows(company):
    return [
        {"country": row.country_id, "country_name": row.country.name, "decimal_places": row.decimal_places,
         "round_off_mode": row.round_off_mode, "round_off_step": fmt(row.round_off_step, row.decimal_places)}
        for row in company.money_settings.select_related("country").order_by("country__name")
    ]


def context_for(request):
    company = company_for(request, request.GET.get("company"))
    config = CompanyConfig.objects.filter(company=company).first() if company else None
    return {
        "company": company,
        "companies": [{"id": c.pk, "name": c.name} for c in scoping.companies_for(request.user).order_by("name")],
        "report_day_start": config.report_day_start.strftime("%H:%M") if config and config.report_day_start else "",
        "money_rows": money_rows(company) if company else [],
        "countries": [{"id": c.pk, "name": c.name} for c in scoping.countries_for(request.user).order_by("name")],
        "places": DECIMAL_PLACES,
        "modes": RoundOffMode.choices,
        "steps": {places: [fmt(step, places) for step in steps] for places, steps in ROUND_OFF_STEPS.items()},
        "dial_code_options": dial_codes.picker_options(DialCode),
        "primary_dial_codes": dial_codes.primary_iso2(company) if company else [],
    }


def primary_codes(data):
    """(DialCode rows in the order sent, errors) from the posted primary dial codes."""
    sent = data.get("primary_dial_codes")
    if sent in (None, ""):
        return [], []
    if not isinstance(sent, list):
        return [], [{"field": "Primary dial codes", "message": "Send a list of country codes."}]
    wanted = []
    for value in sent:
        iso = str(value or "").strip().upper()
        if iso and iso not in wanted:              # a duplicate is dropped, not refused
            wanted.append(iso)
    found = {r.iso2: r for r in DialCode.objects.filter(iso2__in=wanted, is_active=True)}
    missing = [iso for iso in wanted if iso not in found]
    if missing:
        return [], [{"field": "Primary dial codes", "message": f"Unknown or switched off: {', '.join(missing)}."}]
    return [found[iso] for iso in wanted], []


def _time(value):
    if value in (None, ""):
        return None, None
    try:
        return datetime.time.fromisoformat(str(value)[:5]), None
    except ValueError:
        return None, "Enter a time as HH:MM."


def validate(data, countries):
    """(report_day_start, money rows, errors) from a posted page."""
    errors = []
    start, problem = _time(data.get("report_day_start"))
    if problem:
        errors.append({"field": "Business day starts at", "message": problem})
    rows, seen = [], set()
    for index, row in enumerate(data.get("money") or [], start=1):
        where = f"Money row {index}"
        country = str(row.get("country") or "")
        if not country.isdigit() or int(country) not in countries:
            errors.append({"field": where, "message": "Choose a country."})
            continue
        if int(country) in seen:
            errors.append({"field": where, "message": "Each country can have one row only."})
            continue
        seen.add(int(country))
        try:
            places = int(row.get("decimal_places"))
        except (TypeError, ValueError):
            places = None
        if places not in PLACES:
            errors.append({"field": where, "message": "Decimals must be 2 or 3."})
            continue
        mode = row.get("round_off_mode")
        if mode not in RoundOffMode.values:
            errors.append({"field": where, "message": "Choose a round-off mode."})
            continue
        try:
            step = Decimal(str(row.get("round_off_step")))
        except (InvalidOperation, ValueError):
            step = None
        if step not in ROUND_OFF_STEPS[places]:
            errors.append({"field": where, "message": f"Choose a round-off step for {places} decimals."})
            continue
        rows.append({"country_id": int(country), "decimal_places": places, "round_off_mode": mode,
                     "round_off_step": step})
    return start, rows, errors


class ConfigurationSave(WriteView):
    """POST {company, report_day_start, money: [{country, decimal_places,
    round_off_mode, round_off_step}]} -- the whole page."""

    model = CompanyConfig
    page_code = PAGE

    def post(self, request, *args, **kwargs):
        if not self.may("update"):
            return self.refused()
        data = _payload(request)
        company = company_for(request, data.get("company"))
        if company is None:
            return JsonResponse({"ok": False, "code": "invalid",
                                 "errors": [{"field": "Company", "message": "Choose a company."}]}, status=400)
        countries = set(scoping.countries_for(request.user).values_list("pk", flat=True))
        start, rows, errors = validate(data, countries)
        primaries, problems = primary_codes(data)
        errors += problems
        if errors:
            return JsonResponse({"ok": False, "code": "invalid", "errors": errors}, status=400)
        with transaction.atomic():
            config, _ = CompanyConfig.objects.get_or_create(
                company=company, defaults={"created_by": request.user, "modified_by": request.user})
            config.report_day_start, config.modified_by = start, request.user
            config.save()
            kept = {r["country_id"] for r in rows}
            company.money_settings.exclude(country_id__in=kept).delete()
            for row in rows:
                setting = company.money_settings.filter(country_id=row["country_id"]).first() or CompanyMoneySetting(
                    company=company, country_id=row["country_id"], created_by=request.user)
                for field in ("decimal_places", "round_off_mode", "round_off_step"):
                    setattr(setting, field, row[field])
                setting.modified_by = request.user
                setting.save()
            if "primary_dial_codes" in data:
                before = dial_codes.primary_iso2(company)
                if before != [r.iso2 for r in primaries]:
                    company.primary_dial_codes.all().delete()
                    CompanyDialCode.objects.bulk_create([
                        CompanyDialCode(company=company, dial_code=r, position=n)
                        for n, r in enumerate(primaries, start=1)])
                    config.dial_codes_updated_at = timezone.now()
                    config.save(update_fields=["dial_codes_updated_at"])
        return JsonResponse({"ok": True, "message": f"{company.name} configurations saved.",
                             "money": money_rows(company)})

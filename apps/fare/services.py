"""Saving and checking fares.

Views only translate HTTP; the rules live here and in apps.fare.pricing.

save_fare() writes a whole fare -- its branches, rules and seasons -- in one
transaction, after collecting every problem at once:

  - format problems (payload.parse) and the fare's own rules (pricing.validate_spec);
  - scope: the company comes from the user, the vehicle type and branches must
    belong to it, and a rule or season id must belong to this very fare;
  - clashes with other active fares (conflicts), named by fare and branch.

The database's exclusion constraints are the backstop for anything that slips
past -- two people saving at once, or a caller that skips this module. They are
deferred while this function rewrites a fare's rows (so two rules can swap
windows) and checked again before it returns.
"""

import datetime

from django.db import IntegrityError, connection, transaction
from django.db.models import Prefetch, Q
from django.utils import timezone

from apps.company.scoping import branches_for, companies_for
from apps.fare import payload, pricing
from apps.fare.models import Fare, FareBranch, FareLevel, FareRule, FareSeason
from apps.fare.pricing import FareSpec, Price, Rule, Season
from apps.fare.scoping import fares_for
from apps.fleet.models import VehicleType
from apps.fleet.scoping import vehicle_types_for
from core.enums import ApprovalStatus
from core.timezones import business_date_for, zone_for

# Deferred while save_fare rewrites a fare, re-checked before it returns.
DEFERRED = (
    "fare_company_no_overlap", "fare_branch_no_overlap", "fare_season_no_overlap",
    "fare_rule_every_day_fare", "fare_rule_every_day_season",
    "fare_rule_days_fare", "fare_rule_days_season", "fare_rule_single_date",
)

# A database refusal, in words, for the rare save that the checks above could
# not see coming (usually someone else saving a clashing fare at that moment).
CONSTRAINT_MESSAGES = {
    "fare_company_no_overlap": ("Valid from", "Another active company fare for this vehicle type and "
                                              "package now covers these dates. Reload and check."),
    "fare_branch_no_overlap": ("Branches", "Another active fare now covers one of these branches on "
                                           "these dates. Reload and check."),
    "fare_season_no_overlap": ("Seasons", "Two seasons share a date. A date can belong to one season only."),
}
OVERLAP_MESSAGE = ("Special pricing", "Two prices of the same kind cover the same time. Reload and check.")


class Invalid(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(e["message"] for e in errors))
        self.errors = errors


class Stale(Exception):
    """Someone saved this fare after the form was loaded."""

    def __init__(self, fare):
        who = fare.modified_by.display_name if fare.modified_by_id else "Someone"
        when = fare.modified_on.strftime("%-d %b %Y %H:%M") if fare.modified_on else ""
        super().__init__(f"{who} saved this fare{' at ' + when if when else ''}.")
        self.fare = fare


class NotFound(Exception):
    pass


def _issues(issues):
    return [i.as_dict() for i in issues]


def _error(field, message, key=""):
    return {"key": key, "field": field, "message": message}


# -- Saved fare -> spec -----------------------------------------------------------------


def _price(row):
    return Price(row.base_fare, row.grace_minutes, row.concurrent_interval_minutes,
                 row.concurrent_fare, row.concurrent_grace_minutes)


def _rule(row):
    return Rule(f"r{row.pk}", row.kind, row.start_minute, row.end_minute, _price(row),
                frozenset(row.weekdays or ()), row.on_date)


def with_children(queryset):
    return queryset.select_related("vehicle_type", "company").prefetch_related(
        Prefetch("seasons", queryset=FareSeason.objects.order_by("start_date")),
        Prefetch("rules", queryset=FareRule.objects.order_by("start_minute")),
        "branch_links__branch",
    )


def spec_from_fare(fare):
    """A saved fare as pricing data. Expects with_children() prefetching."""
    rules = list(fare.rules.all())
    seasons = tuple(
        Season(f"s{s.pk}", s.name, s.start_date, s.end_date, _price(s),
               tuple(_rule(r) for r in rules if r.season_id == s.pk))
        for s in fare.seasons.all()
    )
    return FareSpec(fare.valid_from, fare.valid_to, fare.package_minutes, _price(fare),
                    tuple(_rule(r) for r in rules if r.season_id is None), seasons)


# -- Clashes with other fares ---------------------------------------------------------------


def _dates(start, end):
    return f"{pricing.date_label(start)} – {pricing.date_label(end)}"


def conflicts(context, spec, exclude_pk=None):
    """Other active fares this one would collide with, as errors naming them."""
    if spec is None or not context["is_active"] or not context["vehicle_type"]:
        return []
    vehicle_type, company = context["vehicle_type"], context["company"]
    same = {"vehicle_type": vehicle_type, "package_minutes": spec.package_minutes, "is_active": True,
            "valid_from__lte": spec.valid_to, "valid_to__gte": spec.valid_from}
    what = f"{vehicle_type} ({spec.package_minutes} min)"
    errors = []
    if context["level"] == FareLevel.COMPANY:
        for other in Fare.objects.filter(company=company, level=FareLevel.COMPANY, **same).exclude(pk=exclude_pk):
            errors.append(_error("Valid from",
                                 f"An active company fare for {what} already covers "
                                 f"{_dates(other.valid_from, other.valid_to)}. Change the dates, or make "
                                 "one of them inactive."))
    elif context["branches"]:
        clashes = {}
        links = (FareBranch.objects.filter(branch__in=context["branches"], **same)
                 .exclude(fare_id=exclude_pk).select_related("branch", "fare"))
        for link in links:
            clashes.setdefault(link.fare, []).append(link.branch.name)
        for other, names in clashes.items():
            errors.append(_error("Branches",
                                 f"{', '.join(sorted(names))} already {'has' if len(names) == 1 else 'have'} "
                                 f"an active fare for {what} covering "
                                 f"{_dates(other.valid_from, other.valid_to)}. Remove "
                                 f"{'it' if len(names) == 1 else 'them'} here, or change the dates."))
    return errors


def holiday_warnings(context, spec, exclude_pk=None):
    """Single dates on a company fare that branches with their own fare for
    the same vehicle type and package will not get -- a branch reads one fare,
    never both."""
    if spec is None or context["level"] != FareLevel.COMPANY or not context["vehicle_type"]:
        return []
    warnings = []
    for rule in spec.rules:
        if rule.kind != pricing.SINGLE_DATE or rule.on_date is None:
            continue
        names = sorted(
            FareBranch.objects.filter(
                branch__company=context["company"], vehicle_type=context["vehicle_type"],
                package_minutes=spec.package_minutes, is_active=True,
                valid_from__lte=rule.on_date, valid_to__gte=rule.on_date,
            ).exclude(fare_id=exclude_pk).values_list("branch__name", flat=True)
        )
        if names:
            warnings.append({
                "key": rule.key, "date": pricing.date_label(rule.on_date), "branches": names,
                "message": f"{pricing.date_label(rule.on_date)} won't reach {', '.join(names)}: "
                           f"{'it uses its' if len(names) == 1 else 'they use their'} own "
                           f"{context['vehicle_type']} fare. Add the date there too.",
            })
    return warnings


# -- Who, what, where: the parts that depend on the user ------------------------------------


def _context(user, meta, fare=None):
    """Resolve company, vehicle type and branches within the user's scope.
    Returns (context, errors). A fare being edited keeps its company."""
    errors = []
    if fare is not None:
        company = fare.company
    elif getattr(user, "sees_every_company", False):
        company = companies_for(user).filter(pk=meta["company"]).first()
        if company is None:
            errors.append(_error("Company", "Choose a company."))
    else:
        company = user.company

    vehicle_type = None
    if company is not None and meta["vehicle_type"]:
        vehicle_type = vehicle_types_for(user).filter(pk=meta["vehicle_type"], company=company).first()
        unchanged = fare is not None and vehicle_type is not None and vehicle_type.pk == fare.vehicle_type_id
        if vehicle_type is None or not (vehicle_type.is_active or unchanged):
            errors.append(_error("Vehicle type", "Choose an active vehicle type of this company."))
            vehicle_type = None

    branches = []
    if company is not None and meta["level"] == FareLevel.BRANCH:
        linked = set(fare.branch_links.values_list("branch_id", flat=True)) if fare else set()
        found = {b.pk: b for b in branches_for(user).filter(pk__in=meta["branches"], company=company)}
        for branch_id in meta["branches"]:
            branch = found.get(branch_id)
            if branch is None or not (branch.is_active or branch_id in linked):
                errors.append(_error("Branches", "Choose active branches of this company."))
                break
            branches.append(branch)

    context = {"company": company, "vehicle_type": vehicle_type, "level": meta["level"],
               "branches": branches, "is_active": meta["is_active"]}
    return context, errors


def _own_ids(meta, fare):
    """A rule or season id must belong to the fare being saved -- never a way
    to reach into another fare's rows."""
    rule_ids = set(fare.rules.values_list("pk", flat=True)) if fare else set()
    season_ids = set(fare.seasons.values_list("pk", flat=True)) if fare else set()
    if any(i and i not in rule_ids for i in meta["rule_ids"].values()) or \
            any(i and i not in season_ids for i in meta["season_ids"].values()):
        return [_error("Special pricing", "Some rows could not be matched to this fare. Reload and try again.")]
    return []


# -- The screen's live check and the Test fare dialog ------------------------------------


def _price_json(price):
    return {"base_fare": str(price.base_fare), "grace_minutes": price.grace_minutes,
            "concurrent_interval_minutes": price.concurrent_interval_minutes,
            "concurrent_fare": str(price.concurrent_fare),
            "concurrent_grace_minutes": price.concurrent_grace_minutes}


def _source_json(source):
    return {"tier": source.tier, "label": pricing.source_label(source),
            "rule": source.rule.key if source.rule else "",
            "season": source.season.key if source.season else "",
            "window": pricing.window_label(source.rule) if source.rule else ""}


def probe(spec, day, minute):
    """What the Test fare dialog shows for one date and time."""
    found = pricing.resolve(spec, day, minute)
    label = f"{pricing.SHORT_DAYS[pricing.WeekDay(day.weekday())]} {pricing.date_label(day)}"
    if found is None:
        return {"found": False, "day": label,
                "message": f"No fare on this date: it is outside the validity "
                           f"({_dates(spec.valid_from, spec.valid_to)}). This vehicle type cannot be rented."}
    schedule = pricing.day_schedule(spec, day)
    here = next(s for s in schedule if s.start <= minute < s.end)
    return {
        "found": True, "day": label, "price": _price_json(found.price), "source": _source_json(found.source),
        "block": f"{pricing.hhmm(here.start)} – {pricing.hhmm(here.end)}",
        "also": [{"label": pricing.source_label(s), "reason": reason} for s, reason in found.also],
        "schedule": [{"start": s.start, "end": s.end, "from": pricing.hhmm(s.start), "to": pricing.hhmm(s.end),
                      "base_fare": str(s.price.base_fare), "source": _source_json(s.source)}
                     for s in schedule],
    }


def check(user, data, test=None):
    """Everything the form shows without saving: errors, rules that never
    apply, notes on single dates inside seasons, holiday warnings and --
    when `test` is {date, time} -- the Test fare answer."""
    parsed = payload.parse(data)
    fare = None
    if parsed.meta["pk"]:
        fare = fares_for(user).filter(pk=parsed.meta["pk"]).first()
    context, scope_errors = _context(user, parsed.meta, fare)
    spec = parsed.spec
    own = _issues(pricing.validate_spec(spec)) if spec else []
    errors = _issues(parsed.errors) + scope_errors + own + conflicts(context, spec, fare.pk if fare else None)
    answer = {"errors": errors, "never": {}, "notes": {}, "holidays": [], "test": None}
    if spec is None or own:
        return answer            # the fare itself is inconsistent: any answer could mislead
    answer["never"] = pricing.never_applies(spec)
    answer["notes"] = pricing.single_date_notes(spec)
    answer["holidays"] = holiday_warnings(context, spec, fare.pk if fare else None)
    if test:
        reader = payload._Reader()
        day = reader.date(test.get("date"), "", "Test date")
        minute = reader.minute(test.get("time"), "", "Test time")
        answer["test"] = ({"found": False, "message": "Enter a date and a time."}
                          if None in (day, minute) else probe(spec, day, minute))
    return answer


# -- Save -----------------------------------------------------------------------------------


def _set_constraints(mode):
    with connection.cursor() as cursor:
        cursor.execute(f"SET CONSTRAINTS {', '.join(DEFERRED)} {mode}")


def _apply_price(row, price):
    row.base_fare, row.grace_minutes = price.base_fare, price.grace_minutes
    row.concurrent_interval_minutes = price.concurrent_interval_minutes
    row.concurrent_fare, row.concurrent_grace_minutes = price.concurrent_fare, price.concurrent_grace_minutes


def _friendly(error):
    name = getattr(getattr(error.__cause__, "diag", None), "constraint_name", "") or ""
    field, message = CONSTRAINT_MESSAGES.get(name, OVERLAP_MESSAGE if name.startswith("fare_rule") else
                                             ("Fare", "This fare conflicts with another change. Reload and try again."))
    return [_error(field, message)]


def save_fare(user, data):
    """Create or update a whole fare. Returns (fare, warnings); raises Invalid,
    Stale or NotFound."""
    parsed = payload.parse(data)
    meta = parsed.meta
    try:
        with transaction.atomic():
            fare = None
            if meta["pk"]:
                fare = fares_for(user).filter(pk=meta["pk"]).first()
                if fare is None:
                    raise NotFound()
            context, scope_errors = _context(user, meta, fare)

            # Lock order, always: vehicle type, then the fare. Serialises every
            # save for one vehicle type, so two people cannot both pass the
            # overlap check. no_key: other tables' foreign keys stay unblocked.
            if context["vehicle_type"]:
                VehicleType.objects.select_for_update(no_key=True).filter(pk=context["vehicle_type"].pk).first()
            if fare is not None:
                fare = Fare.objects.select_for_update(of=("self",)).select_related("modified_by").get(pk=fare.pk)
                if fare.lock_version != meta["lock_version"]:
                    raise Stale(fare)

            spec = parsed.spec
            errors = (_issues(parsed.errors) + scope_errors + _own_ids(meta, fare)
                      + (_issues(pricing.validate_spec(spec)) if spec else [])
                      + conflicts(context, spec, fare.pk if fare else None))
            if errors:
                raise Invalid(errors)

            _set_constraints("DEFERRED")
            fare = _write(user, fare, context, spec, meta)
            _set_constraints("IMMEDIATE")
    except IntegrityError as error:
        raise Invalid(_friendly(error)) from error

    saved = with_children(Fare.objects.filter(pk=fare.pk)).get()
    saved_spec = spec_from_fare(saved)
    warnings = {"never": pricing.never_applies(saved_spec),
                "holidays": holiday_warnings(context, saved_spec, saved.pk)}
    return saved, warnings


def _write(user, fare, context, spec, meta):
    keep_rules = {i for i in meta["rule_ids"].values() if i}
    keep_seasons = {i for i in meta["season_ids"].values() if i}
    branch_ids = {b.pk for b in context["branches"]}

    if fare is not None:
        # Remove before updating the fare: a link left behind would take the
        # fare's new values by cascade (a company-level fare cannot have any).
        fare.branch_links.exclude(branch_id__in=branch_ids).delete()
        fare.rules.exclude(pk__in=keep_rules).delete()
        fare.seasons.exclude(pk__in=keep_seasons).delete()
    else:
        # Approval: same as every other master for now -- saved approved, with
        # the Active / Inactive the form chose (apply_approval_defaults would
        # force it active).
        fare = Fare(company=context["company"], created_by=user,
                    want_approval=False, approval_status=ApprovalStatus.APPROVED)

    fare.vehicle_type = context["vehicle_type"]
    fare.level = context["level"]
    fare.valid_from, fare.valid_to = spec.valid_from, spec.valid_to
    fare.package_minutes = spec.package_minutes
    fare.is_active = context["is_active"]
    _apply_price(fare, spec.base)
    fare.modified_by = user
    fare.lock_version = (fare.lock_version or 0) + (1 if fare.pk else 0)
    fare.save()

    existing_seasons = {s.pk: s for s in fare.seasons.all()}
    season_rows = {}
    for season in spec.seasons:
        row = existing_seasons.get(meta["season_ids"].get(season.key)) or FareSeason(fare=fare)
        row.name, row.start_date, row.end_date = season.name.strip(), season.start, season.end
        _apply_price(row, season.base)
        row.save()
        season_rows[season.key] = row

    existing_rules = {r.pk: r for r in fare.rules.all()}
    owned = [(rule, None) for rule in spec.rules] + [(rule, season_rows[s.key]) for s in spec.seasons for rule in s.rules]
    for rule, season_row in owned:
        row = existing_rules.get(meta["rule_ids"].get(rule.key)) or FareRule(fare=fare)
        row.season = season_row
        row.kind = rule.kind
        row.weekdays = sorted(rule.weekdays)
        row.on_date = rule.on_date
        row.start_minute, row.end_minute = rule.start, rule.end
        _apply_price(row, rule.price)
        row.save()

    linked = set(fare.branch_links.values_list("branch_id", flat=True))
    for branch in context["branches"]:
        if branch.pk not in linked:
            FareBranch.objects.create(fare=fare, branch=branch)
    return fare


# -- The operator app's fare download (apps/fare/api.py) --------------------------------
#
# Every fare package of one vehicle type, sent whole: the app runs the
# precedence itself (design/fares and offers/fare-schema.md section 4). The
# branch is the caller's -- taken from its session, never from the request.


class UnknownVehicleType(Exception):
    """Not this branch's company's, or not active and approved."""


RULE_ORDER = {"single_date": 0, "selected_days": 1, "every_day": 2}


def _device_price(row):
    return {
        "base_fare": str(row.base_fare), "grace_minutes": row.grace_minutes,
        "concurrent_interval_minutes": row.concurrent_interval_minutes,
        "concurrent_fare": str(row.concurrent_fare),
        "concurrent_grace_minutes": row.concurrent_grace_minutes,
    }


def _device_rules(rules):
    rules = sorted(rules, key=lambda r: (RULE_ORDER[r.kind], r.on_date or datetime.date.min, r.start_minute))
    return [
        {
            "id": r.pk, "kind": r.kind,
            "on_date": r.on_date.isoformat() if r.on_date else None,
            "weekdays": sorted(r.weekdays or []),
            "start": pricing.hhmm(r.start_minute), "end": pricing.hhmm(r.end_minute),
            "price": _device_price(r),
        }
        for r in rules
    ]


def device_fares(branch, vehicle_type_id, *, at=None):
    """The fare download for one vehicle type at `branch`: active, approved
    fares that have not ended, company-level and this branch's own. Both
    levels are sent; the app lets the branch's fare win."""
    company = branch.company
    vehicle_type = (VehicleType.objects.select_related("category")
                    .filter(pk=vehicle_type_id, company=company, is_active=True,
                            approval_status=ApprovalStatus.APPROVED).first())
    if vehicle_type is None:
        raise UnknownVehicleType()

    now = at or timezone.now()
    today = business_date_for(company, now)
    fares = with_children(
        Fare.objects.filter(company=company, vehicle_type=vehicle_type, is_active=True,
                            approval_status=ApprovalStatus.APPROVED, valid_to__gte=today)
        .filter(Q(level=FareLevel.COMPANY) | Q(level=FareLevel.BRANCH, branch_links__branch=branch))
        .distinct()
        .order_by("package_minutes", "valid_from", "level")
    )

    out = []
    for fare in fares:
        rules = list(fare.rules.all())
        out.append({
            "fare_id": fare.pk, "version": fare.lock_version, "level": fare.level,
            "package_minutes": fare.package_minutes,
            "valid_from": fare.valid_from.isoformat(), "valid_to": fare.valid_to.isoformat(),
            "base_price": _device_price(fare),
            "special_prices": _device_rules(r for r in rules if r.season_id is None),
            "seasons": [
                {
                    "id": s.pk, "name": s.name,
                    "start_date": s.start_date.isoformat(), "end_date": s.end_date.isoformat(),
                    "base_price": _device_price(s),
                    "special_prices": _device_rules(r for r in rules if r.season_id == s.pk),
                }
                for s in fare.seasons.all()
            ],
        })

    return {
        "generated_at": now.astimezone(zone_for(company)).isoformat(timespec="seconds"),
        "business_date": today.isoformat(),
        "company": {"id": company.pk, "code": company.short_code, "timezone": company.timezone,
                    "tax_type": company.tax_type, "discount_type": company.discount_type},
        "branch": {"id": branch.pk, "code": branch.short_code, "name": branch.name},
        "vehicle_type": {
            "id": vehicle_type.pk, "name": vehicle_type.vehicle_type_name,
            "code": vehicle_type.vehicle_type_code, "category": vehicle_type.category.category_name,
            "tax_percentage": (str(vehicle_type.tax_percentage)
                               if vehicle_type.tax_percentage is not None else None),
        },
        "fares": out,
    }

"""Incentive calculation (design/09-incentives.md sections 2, 4 and 6;
design/11-target-profiles.md P6-P7).

A run is one station's *period* of one target profile -- a month, a day, a
custom range or a financial year -- paid on that profile's scheme.

  I1  a station pays for a period only when its scheme's *pays when* holds
      (default achieved >= target), the period has ended, and it has a target;
  I2  basis = achieved / working days in the period (a day with >= 1 person
      rostered Working there) -- the same figure for every working day;
  I3  each working day, that day's roster decides who gets which component;
  I5  an employee is paid station by station; the payout month's total is the sum;
  I6  a run is a draft until approved; approved is final.

Achieved is Targets' figure (apps/targets/services.achieved_for), so the
target page and the incentive never disagree. Calls that name no profile mean
the station's monthly sales target, as before profiles.
"""

from collections import Counter, defaultdict
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.crew.duty import role_of
from apps.crew.models import CashierType, DutyRoster, DutyRosterDayType
from apps.incentives import formula
from apps.incentives.models import APPLIES, AppliesTo, Line, Payout, Run, RunStatus, Scheme
from apps.incentives.variables import CATALOGUE, MONTH_NAMES
from apps.targets import periods
from apps.targets import services as targets
from apps.targets.models import PeriodType, Target, TargetKind
from core.money import places_for

ZERO = Decimal("0")


class IncentiveError(Exception):
    """A refusal, with a field and a message for the person."""

    def __init__(self, field, message):
        super().__init__(message)
        self.field, self.message = field, message


def _q(value, places):
    return Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


# -- Schemes -------------------------------------------------------------------------------

def scheme_for(company, branch, first, profile=None):
    """The active scheme of a profile for a station's period: one naming the
    station wins over an all-stations one of the same profile. Two at the same
    level is a configuration error. No profile: the monthly sales one's."""
    found = Scheme.objects.filter(company=company, is_active=True, effective_from__lte=first)
    if profile is not None:
        found = found.filter(profile=profile)
    else:
        found = found.filter(profile__metric=TargetKind.SALES, profile__period_type=PeriodType.MONTHLY)
    schemes = list(found.filter(Q(effective_to__isnull=True) | Q(effective_to__gte=first))
                   .prefetch_related("branches", "variables", "components"))
    own = [s for s in schemes if any(b.pk == branch.pk for b in s.branches.all())]
    general = [s for s in schemes if not s.branches.all()]
    pick = own or general
    if len(pick) > 1:
        raise IncentiveError("Scheme", f"More than one active scheme covers {branch.name} from {first:%-d %b %Y}: "
                                       + ", ".join(s.name for s in pick) + ".")
    return pick[0] if pick else None


def validate(pays_when, variables, components):
    """Every problem with a scheme's formulas, as [{field, message}].
    `variables`: [(name, formula)]; `components`: [(name, applies_to, formula)]."""
    errors = []
    try:
        formula.check(pays_when, MONTH_NAMES)
    except formula.FormulaError as error:
        errors.append({"field": "Pays when", "message": f"{error} It may use only: {', '.join(MONTH_NAMES)}."})
    known = set(CATALOGUE)
    for name, text in variables:
        label = f"Variable {name or '(no name)'}"
        if not formula.is_name(name):
            errors.append({"field": label, "message": "Name it with lower-case letters, digits and _ only."})
            continue
        if name in known:
            errors.append({"field": label, "message": "That name is already a variable."})
            continue
        try:
            formula.check(text, known)
        except formula.FormulaError as error:
            errors.append({"field": label, "message": str(error)})
        known.add(name)
    if not components:
        errors.append({"field": "Components", "message": "Add at least one component."})
    for name, applies_to, text in components:
        label = f"Component {name or '(no name)'}"
        if not (name or "").strip():
            errors.append({"field": "Component", "message": "Give each component a name."})
        if applies_to not in AppliesTo.values:
            errors.append({"field": label, "message": "Choose who it applies to."})
        try:
            formula.check(text, known)
        except formula.FormulaError as error:
            errors.append({"field": label, "message": str(error)})
    return errors


# -- The roster --------------------------------------------------------------------------------

def person_role(row):
    """primary / secondary / labour for a Working roster row, else None."""
    role = role_of(row)
    if role == "cashier":
        return "primary" if row.cashier_type == CashierType.PRIMARY else "secondary"
    if role == "labour":
        return "labour"
    return None


def roster_days(branch, first, last):
    """{date: [(employee, role)]} -- who was rostered Working at the station."""
    rows = (DutyRoster.objects.filter(branch=branch, date__gte=first, date__lte=last,
                                      day_type=DutyRosterDayType.WORKING)
            .select_related("employee__designation").order_by("date", "employee__first_name", "pk"))
    days = defaultdict(list)
    for row in rows:
        role = person_role(row)
        if role:
            days[row.date].append((row.employee, role))
    for people in days.values():
        # A day with cashiers but none primary (rows saved before the type
        # existed): the first cashier is the primary, as the roster would promote.
        if not any(r == "primary" for _, r in people):
            for index, (employee, role) in enumerate(people):
                if role == "secondary":
                    people[index] = (employee, "primary")
                    break
    return dict(days)


def day_counts(people):
    roles = Counter(role for _, role in people)
    return {"cashiers": Decimal(roles["primary"] + roles["secondary"]), "labour": Decimal(roles["labour"]),
            "tier2": Decimal(roles["secondary"] + roles["labour"]), "staff": Decimal(len(people))}


def person_values(role, days_worked):
    return {"role": role, "is_primary": Decimal(role == "primary"), "is_secondary": Decimal(role == "secondary"),
            "is_labour": Decimal(role == "labour"), "days_worked": Decimal(days_worked)}


# -- The period ------------------------------------------------------------------------------

def sales_target(branch, first, profile=None):
    """The station's target that a run is worked out on: the profile's period
    starting `first`, or (no profile) its monthly sales target."""
    found = Target.objects.filter(branch=branch, period_start=first).select_related("profile", "company")
    if profile is not None:
        return found.filter(profile=profile).first()
    return found.filter(kind=TargetKind.SALES, period_type=PeriodType.MONTHLY).first()


def month_achieved(company, branch, first, last):
    found = targets.achieved_by_day(company, [branch.pk], first, last)
    return sum((v for (b, _), v in found.items() if b == branch.pk), ZERO)


def target_achieved(target, today):
    """Targets' own figure for the period, so the two pages never disagree:
    AED for sales, unique customers for customers; up to today while it runs."""
    return targets.achieved_for([target], today).get(target.pk, ZERO)


def month_values(target_amount, achieved, work_days, first, last=None):
    """The period's figures for the formulas. days_in_month is kept as an
    alias of days_in_period so schemes written before profiles still work."""
    last = last or targets.month_bounds(first.year, first.month)[1]
    basis = achieved / work_days if work_days else ZERO
    pct = achieved * 100 / target_amount if target_amount else ZERO
    days = Decimal((last - first).days + 1)
    return {"achieved": achieved, "target": target_amount, "achievement_pct": pct, "work_days": Decimal(work_days),
            "basis": basis, "days_in_period": days, "days_in_month": days}


def payout_month(scheme, target):
    """The first of the month the run is paid in (11 P7): the month its period
    ends, or the month the financial year containing that end finishes."""
    end = target.period_end
    if scheme.payout == Payout.FINANCIAL_YEAR_END:
        end = periods.financial_year(end, periods.fy_start_month(target.company))[1]
    return end.replace(day=1)


def period_label(target_or_run):
    item = target_or_run
    kind = item.period_type if hasattr(item, "period_type") else item.profile.period_type
    return periods.period_label(kind, item.period_start, item.period_end)


def _compile(scheme):
    return compile_parts([(v.name, v.formula) for v in scheme.variables.all()], list(scheme.components.all()))


def compile_parts(variables, components):
    """(variables, components) ready to evaluate. `components` are
    SchemeComponent rows, or unsaved ones built from a form (Try it)."""
    return ([(name, formula.parse(text)) for name, text in variables],
            [(c, formula.parse(c.formula)) for c in components])


def person_lines(scheme_parts, month, counts, role, days_worked, places):
    """[(component, amount, values)] for one person on one day."""
    variables, components = scheme_parts
    values = {**month, **counts, **person_values(role, days_worked)}
    for name, tree in variables:
        try:
            values[name] = formula.evaluate(tree, values)
        except formula.FormulaError as error:
            raise IncentiveError(f"Variable {name}", str(error)) from None
    out = []
    for component, tree in components:
        if role not in APPLIES[component.applies_to]:
            continue
        try:
            amount = _q(formula.evaluate(tree, values), places)
        except formula.FormulaError as error:
            raise IncentiveError(f"Component {component.name}", str(error)) from None
        if amount:
            out.append((component, amount, values))
    return out


def _json(values):
    return {k: (format(v.normalize(), "f") if isinstance(v, Decimal) else v) for k, v in values.items()}


def eligibility(branch, first, now=None, profile=None):
    """What the Calculate list shows for a station's period: target, achieved,
    the run, and whether Calculate is allowed (with the reason when not)."""
    company = branch.company
    today = targets.today_for(company, now)
    target = sales_target(branch, first, profile)
    profile = target.profile if target else profile
    last = target.period_end if target else targets.month_bounds(first.year, first.month)[1]
    run = (Run.objects.filter(branch=branch, period_start=first, profile=profile).first()
           if profile else None)
    achieved = target_achieved(target, today) if target and first <= today else ZERO
    met = bool(target) and achieved >= target.amount
    reason = ""
    if target is None:
        reason = "No target"
    elif today <= last:
        reason = "Period not over"
    elif run is not None and run.is_approved:
        reason = "Approved"
    else:
        try:
            scheme = scheme_for(company, branch, first, profile)
        except IncentiveError as error:
            scheme, reason = None, error.message
        if not reason and scheme is None:
            reason = "No active scheme"
        if not reason:
            # the same working days as calculate(), so the two never disagree
            values = month_values(target.amount, achieved, len(roster_days(branch, first, last)), first, last)
            try:
                if not formula.evaluate(scheme.pays_when, values):
                    reason = "Target not met"
            except formula.FormulaError as error:
                reason = f"Scheme error: {error}"
    return {"branch": branch, "target": target, "achieved": achieved, "met": met, "run": run,
            "pct": targets.percent(achieved, target.amount) if target else None,
            "can_calculate": not reason, "reason": reason, "ended": today > last, "profile": profile,
            "label": period_label(target) if target else ""}


@transaction.atomic
def calculate(branch, first, user, now=None, profile=None):
    """Work out (or again) a station's period of a profile as a draft run.
    Refused unless the period has ended, the station has a target, a scheme
    of the profile applies and its *pays when* holds, and the run is not
    approved. No profile: the station's monthly sales target."""
    company = branch.company
    today = targets.today_for(company, now)
    target = sales_target(branch, first, profile)
    if target is None and profile is None and today <= targets.month_bounds(first.year, first.month)[1]:
        raise IncentiveError("Period", f"{first:%B %Y} is not over yet.")
    if target is None:
        what = f"{profile.name} target" if profile else "sales target"
        raise IncentiveError("Target", f"{branch.name} has no {what} starting {first:%-d %b %Y}.")
    profile, last, label = target.profile, target.period_end, period_label(target)
    if today <= last:
        raise IncentiveError("Period", f"{label} is not over yet.")
    existing = Run.objects.select_for_update().filter(branch=branch, profile=profile, period_start=first).first()
    if existing is not None and existing.is_approved:
        raise IncentiveError("Run", "This period is approved; it can't be calculated again.")
    scheme = scheme_for(company, branch, first, profile)
    if scheme is None:
        raise IncentiveError("Scheme", f"No active scheme of “{profile.name}” covers {branch.name} for {label}.")
    achieved = target_achieved(target, today)
    days = roster_days(branch, first, last)
    month = month_values(target.amount, achieved, len(days), first, last)
    try:
        pays = formula.evaluate(scheme.pays_when, month)
    except formula.FormulaError as error:
        raise IncentiveError("Pays when", str(error)) from None
    if not pays:
        raise IncentiveError("Target", f"{branch.name} did not meet the scheme's condition ({scheme.pays_when}) "
                                       f"for {label}.")
    try:
        parts = _compile(scheme)
    except formula.FormulaError as error:
        raise IncentiveError("Scheme", str(error)) from None
    places = places_for(company, branch)
    worked = Counter(employee.pk for people in days.values() for employee, _ in people)
    lines = []
    for date in sorted(days):
        people = days[date]
        counts = day_counts(people)
        for employee, role in people:
            for component, amount, values in person_lines(parts, month, counts, role, worked[employee.pk], places):
                lines.append(Line(employee=employee, date=date, role=role, component=component.name, amount=amount,
                                  inputs=_json(values)))
    run, _ = Run.objects.update_or_create(
        branch=branch, profile=profile, period_start=first,
        defaults={"company": company, "period_end": last, "payout_month": payout_month(scheme, target),
                  "scheme": scheme, "target_amount": target.amount,
                  "achieved": achieved, "work_days": len(days), "basis": _q(month["basis"], 3),
                  "total": sum((ln.amount for ln in lines), ZERO), "status": RunStatus.DRAFT,
                  "calculated_at": now or timezone.now(), "calculated_by": user, "modified_by": user})
    run.lines.all().delete()
    for line in lines:
        line.run = run
    Line.objects.bulk_create(lines)
    return run


def approve(run, user, now=None):
    if run.is_approved:
        raise IncentiveError("Run", "This period is already approved.")
    run.status, run.approved_at, run.approved_by, run.modified_by = (RunStatus.APPROVED, now or timezone.now(),
                                                                      user, user)
    run.save(update_fields=["status", "approved_at", "approved_by", "modified_by", "modified_on"])
    return run


# -- Reading -----------------------------------------------------------------------------------

def run_by_day(run):
    """[{date, people: [{employee, role, components: [(name, amount)], amount}], total}]."""
    days = defaultdict(lambda: defaultdict(lambda: {"components": [], "amount": ZERO}))
    for line in run.lines.select_related("employee"):
        person = days[line.date][line.employee]
        person["role"] = line.role
        person["components"].append((line.component, line.amount))
        person["amount"] += line.amount
    out = []
    for date in sorted(days):
        people = [{"employee": e, **p} for e, p in days[date].items()]
        people.sort(key=lambda p: ({"primary": 0, "secondary": 1, "labour": 2}.get(p["role"], 3),
                                   p["employee"].full_name))
        out.append({"date": date, "people": people, "total": sum((p["amount"] for p in people), ZERO)})
    return out


def run_by_employee(run):
    """[{employee, days, amount, roles}] for one run, highest first."""
    people = defaultdict(lambda: {"dates": set(), "amount": ZERO, "roles": Counter()})
    for line in run.lines.select_related("employee"):
        person = people[line.employee]
        if line.date not in person["dates"]:
            person["roles"][line.role] += 1
        person["dates"].add(line.date)
        person["amount"] += line.amount
    out = [{"employee": e, "days": len(p["dates"]), "amount": p["amount"], "roles": dict(p["roles"])}
           for e, p in people.items()]
    out.sort(key=lambda r: (-r["amount"], r["employee"].full_name))
    return out


def employee_totals(company, branches, first, *, approved_only=False, profile=None):
    """Every employee with lines paid in the month `first` across these
    stations (I5, 11 P7): one entry per station and profile, and the approved
    / draft totals. A daily special pays with its month, an annual profile at
    the financial year's end -- whichever its scheme says."""
    lines = (Line.objects.filter(run__company=company, run__branch__in=branches, run__payout_month=first)
             .select_related("employee", "run__branch", "run__profile"))
    if approved_only:                 # the employee app's Team Incentive (24 Nov 2026)
        lines = lines.filter(run__status=RunStatus.APPROVED)
    if profile is not None:
        lines = lines.filter(run__profile=profile)
    people = {}
    for line in lines:
        person = people.setdefault(line.employee.pk, {"employee": line.employee, "stations": {},
                                                      "approved": ZERO, "draft": ZERO, "dates": set()})
        station = person["stations"].setdefault(line.run_id, {
            "branch": line.run.branch, "run": line.run, "profile": line.run.profile,
            "label": period_label(line.run), "amount": ZERO, "dates": set()})
        station["amount"] += line.amount
        station["dates"].add(line.date)
        person["dates"].add((line.run.branch_id, line.date))
        if line.run.is_approved:
            person["approved"] += line.amount
        else:
            person["draft"] += line.amount
    out = []
    for person in people.values():
        stations = sorted(person["stations"].values(),
                          key=lambda s: (s["branch"].name, s["profile"].name.lower(), s["run"].period_start))
        for s in stations:
            s["days"] = len(s.pop("dates"))
        out.append({"employee": person["employee"], "stations": stations, "approved": person["approved"],
                    "draft": person["draft"], "total": person["approved"] + person["draft"],
                    "days": len(person["dates"])})
    out.sort(key=lambda r: (-r["total"], r["employee"].full_name))
    return out


def try_scheme(scheme, branch, day, now=None):
    """A saved scheme on one real station-day (see try_parts)."""
    return try_parts(scheme.pays_when, _compile(scheme), branch, day, now, profile=scheme.profile)


def try_parts(pays_when, parts, branch, day, now=None, profile=None):
    """Formulas on one real station-day, before anyone is paid: the period's
    figures so far (or the whole period once ended), that day's roster, and
    each person's amount. Says whether *pays when* holds, but never gates.
    The period is the profile's one containing `day` (no profile: its month)."""
    company = branch.company
    today = targets.today_for(company, now)
    target = None
    if profile is not None:
        target = (Target.objects.filter(branch=branch, profile=profile, period_start__lte=day, period_end__gte=day)
                  .select_related("company", "profile").first())
        span = (target.period_start, target.period_end) if target else periods.period_containing(profile, day)
    else:
        span = None
    first, last = span or targets.month_bounds(day.year, day.month)
    if profile is None:
        target = sales_target(branch, first)
    if target is not None:
        achieved = target_achieved(target, today) if first <= today else ZERO
    else:
        achieved = month_achieved(company, branch, first, min(last, today)) if first <= today else ZERO
    days = roster_days(branch, first, last)
    month = month_values(target.amount if target else ZERO, achieved, len(days), first, last)
    people = days.get(day, [])
    counts = day_counts(people)
    worked = Counter(employee.pk for p in days.values() for employee, _ in p)
    places = places_for(company, branch)
    rows = []
    for employee, role in people:
        found = person_lines(parts, month, counts, role, worked[employee.pk], places)
        rows.append({"employee": employee, "role": role,
                     "components": [(c.name, amount) for c, amount, _ in found],
                     "amount": sum((amount for _, amount, _ in found), ZERO)})
    try:
        pays = bool(formula.evaluate(pays_when, month))
    except formula.FormulaError:
        pays = False
    return {"month": month, "counts": counts, "people": rows, "pays_now": pays, "target": target,
            "date": day, "has_roster": bool(people)}


def copy_scheme(scheme, user):
    """A new, inactive scheme with the same formulas (an approved run's scheme
    can't change -- copy it and change the copy)."""
    with transaction.atomic():
        twin = Scheme.objects.create(company=scheme.company, profile=scheme.profile, payout=scheme.payout,
                                     name=f"{scheme.name} (copy)"[:100],
                                     effective_from=scheme.effective_from, effective_to=scheme.effective_to,
                                     pays_when=scheme.pays_when, is_active=False, notes=scheme.notes,
                                     created_by=user, modified_by=user)
        twin.branches.set(scheme.branches.all())
        for v in scheme.variables.all():
            twin.variables.create(position=v.position, name=v.name, formula=v.formula)
        for c in scheme.components.all():
            twin.components.create(position=c.position, name=c.name, applies_to=c.applies_to, formula=c.formula)
    return twin



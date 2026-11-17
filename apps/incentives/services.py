"""Incentive calculation (design/09-incentives.md sections 2, 4 and 6).

  I1  a station pays for a month only when its scheme's *pays when* holds
      (default achieved >= target), the month has ended, and it has a sales target;
  I2  basis = month achieved / working days (a day with >= 1 person rostered
      Working there) -- the same figure for every working day;
  I3  each working day, that day's roster decides who gets which component;
  I5  an employee is paid station by station, the month total is the sum;
  I6  a run is a draft until approved; approved is final.

Achieved is Targets' figure (apps/targets/services.achieved_by_day), so the
target page and the incentive never disagree.
"""

from collections import Counter, defaultdict
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.crew.duty import role_of
from apps.crew.models import CashierType, DutyRoster, DutyRosterDayType
from apps.incentives import formula
from apps.incentives.models import APPLIES, AppliesTo, Line, Run, RunStatus, Scheme
from apps.incentives.variables import CATALOGUE, MONTH_NAMES
from apps.targets import services as targets
from apps.targets.models import Target, TargetKind
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

def scheme_for(company, branch, first):
    """The active scheme for a station's month: one naming the station wins
    over an all-stations one. Two at the same level is a configuration error."""
    schemes = list(Scheme.objects.filter(company=company, is_active=True, effective_from__lte=first)
                   .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=first))
                   .prefetch_related("branches", "variables", "components"))
    own = [s for s in schemes if any(b.pk == branch.pk for b in s.branches.all())]
    general = [s for s in schemes if not s.branches.all()]
    pick = own or general
    if len(pick) > 1:
        raise IncentiveError("Scheme", f"More than one active scheme covers {branch.name} in {first:%B %Y}: "
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


# -- The month -------------------------------------------------------------------------------

def sales_target(branch, first):
    return Target.objects.filter(branch=branch, kind=TargetKind.SALES, period_start=first).first()


def month_achieved(company, branch, first, last):
    found = targets.achieved_by_day(company, [branch.pk], first, last)
    return sum((v for (b, _), v in found.items() if b == branch.pk), ZERO)


def month_values(target_amount, achieved, work_days, first):
    last = targets.month_bounds(first.year, first.month)[1]
    basis = achieved / work_days if work_days else ZERO
    pct = achieved * 100 / target_amount if target_amount else ZERO
    return {"achieved": achieved, "target": target_amount, "achievement_pct": pct, "work_days": Decimal(work_days),
            "basis": basis, "days_in_month": Decimal((last - first).days + 1)}


def _compile(scheme):
    variables = [(v.name, formula.parse(v.formula)) for v in scheme.variables.all()]
    components = [(c, formula.parse(c.formula)) for c in scheme.components.all()]
    return variables, components


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


def eligibility(branch, first, now=None):
    """What the Calculate list shows for a station's month: target, achieved,
    the run, and whether Calculate is allowed (with the reason when not)."""
    company = branch.company
    today = targets.today_for(company, now)
    last = targets.month_bounds(first.year, first.month)[1]
    target = sales_target(branch, first)
    run = Run.objects.filter(branch=branch, period_start=first).first()
    achieved = month_achieved(company, branch, first, min(last, today)) if first <= today else ZERO
    met = bool(target) and achieved >= target.amount
    reason = ""
    if target is None:
        reason = "No target"
    elif today <= last:
        reason = "Month not over"
    elif run is not None and run.is_approved:
        reason = "Approved"
    else:
        try:
            scheme = scheme_for(company, branch, first)
        except IncentiveError as error:
            scheme, reason = None, error.message
        if not reason and scheme is None:
            reason = "No active scheme"
        if not reason:
            values = month_values(target.amount, achieved, 1, first)
            try:
                if not formula.evaluate(scheme.pays_when, values):
                    reason = "Target not met"
            except formula.FormulaError as error:
                reason = f"Scheme error: {error}"
    return {"branch": branch, "target": target, "achieved": achieved, "met": met, "run": run,
            "pct": targets.percent(achieved, target.amount) if target else None,
            "can_calculate": not reason, "reason": reason, "ended": today > last}


@transaction.atomic
def calculate(branch, first, user, now=None):
    """Work out (or again) a station's month as a draft run. Refused unless
    the month has ended, the station has a sales target, a scheme applies and
    its *pays when* holds, and the month is not approved."""
    company = branch.company
    today = targets.today_for(company, now)
    last = targets.month_bounds(first.year, first.month)[1]
    if today <= last:
        raise IncentiveError("Month", f"{first:%B %Y} is not over yet.")
    existing = Run.objects.select_for_update().filter(branch=branch, period_start=first).first()
    if existing is not None and existing.is_approved:
        raise IncentiveError("Run", "This month is approved; it can't be calculated again.")
    target = sales_target(branch, first)
    if target is None:
        raise IncentiveError("Target", f"{branch.name} has no sales target for {first:%B %Y}.")
    scheme = scheme_for(company, branch, first)
    if scheme is None:
        raise IncentiveError("Scheme", f"No active scheme covers {branch.name} in {first:%B %Y}.")
    achieved = month_achieved(company, branch, first, last)
    days = roster_days(branch, first, last)
    month = month_values(target.amount, achieved, len(days), first)
    try:
        pays = formula.evaluate(scheme.pays_when, month)
    except formula.FormulaError as error:
        raise IncentiveError("Pays when", str(error)) from None
    if not pays:
        raise IncentiveError("Target", f"{branch.name} did not meet the scheme's condition ({scheme.pays_when}) "
                                       f"for {first:%B %Y}.")
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
        branch=branch, period_start=first,
        defaults={"company": company, "period_end": last, "scheme": scheme, "target_amount": target.amount,
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
        raise IncentiveError("Run", "This month is already approved.")
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


def employee_totals(company, branches, first):
    """Every employee with lines in the month across these stations (I5):
    their per-station amounts and the approved / draft totals."""
    lines = (Line.objects.filter(run__company=company, run__branch__in=branches, run__period_start=first)
             .select_related("employee", "run__branch"))
    people = {}
    for line in lines:
        person = people.setdefault(line.employee.pk, {"employee": line.employee, "stations": {},
                                                      "approved": ZERO, "draft": ZERO, "dates": set()})
        station = person["stations"].setdefault(line.run.branch_id, {
            "branch": line.run.branch, "run": line.run, "amount": ZERO, "dates": set()})
        station["amount"] += line.amount
        station["dates"].add(line.date)
        person["dates"].add((line.run.branch_id, line.date))
        if line.run.is_approved:
            person["approved"] += line.amount
        else:
            person["draft"] += line.amount
    out = []
    for person in people.values():
        stations = sorted(person["stations"].values(), key=lambda s: s["branch"].name)
        for s in stations:
            s["days"] = len(s.pop("dates"))
        out.append({"employee": person["employee"], "stations": stations, "approved": person["approved"],
                    "draft": person["draft"], "total": person["approved"] + person["draft"],
                    "days": len(person["dates"])})
    out.sort(key=lambda r: (-r["total"], r["employee"].full_name))
    return out


def try_scheme(scheme, branch, day, now=None):
    """The scheme on one real station-day, before anyone is paid: the month's
    figures so far (or the whole month once ended), that day's roster, and
    each person's amount with the values used. Ignores *pays when*."""
    company = branch.company
    first = day.replace(day=1)
    last = targets.month_bounds(first.year, first.month)[1]
    today = targets.today_for(company, now)
    target = sales_target(branch, first)
    achieved = month_achieved(company, branch, first, min(last, today)) if first <= today else ZERO
    days = roster_days(branch, first, last)
    month = month_values(target.amount if target else ZERO, achieved, len(days), first)
    people = days.get(day, [])
    counts = day_counts(people)
    worked = Counter(employee.pk for p in days.values() for employee, _ in p)
    parts = _compile(scheme)
    places = places_for(company, branch)
    rows = []
    for employee, role in people:
        found = person_lines(parts, month, counts, role, worked[employee.pk], places)
        rows.append({"employee": employee, "role": role,
                     "components": [(c.name, amount) for c, amount, _ in found],
                     "amount": sum((amount for _, amount, _ in found), ZERO)})
    try:
        pays = bool(formula.evaluate(scheme.pays_when, month))
    except formula.FormulaError:
        pays = False
    return {"month": month, "counts": counts, "people": rows, "pays_now": pays, "target": target,
            "date": day, "has_roster": bool(people)}


def copy_scheme(scheme, user):
    """A new, inactive scheme with the same formulas (an approved run's scheme
    can't change -- copy it and change the copy)."""
    with transaction.atomic():
        twin = Scheme.objects.create(company=scheme.company, name=f"{scheme.name} (copy)"[:100],
                                     effective_from=scheme.effective_from, effective_to=scheme.effective_to,
                                     pays_when=scheme.pays_when, is_active=False, notes=scheme.notes,
                                     created_by=user, modified_by=user)
        twin.branches.set(scheme.branches.all())
        for v in scheme.variables.all():
            twin.variables.create(position=v.position, name=v.name, formula=v.formula)
        for c in scheme.components.all():
            twin.components.create(position=c.position, name=c.name, applies_to=c.applies_to, formula=c.formula)
    return twin



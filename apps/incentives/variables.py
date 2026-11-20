"""The variables a scheme's formulas may use (design/09-incentives.md 4.2).

The server defines them -- an admin picks from this list and can build
*derived* variables on top (SchemeVariable), but cannot invent a new figure:
only the server knows where its data lives. A new data variable is an entry
here plus its computation in services.calculate.

`scope` says when the value is known:
  month   once per station-month (pays_when may use only these)
  day     per working day at the station (who else was rostered)
  person  per employee per day (their role) or month (their days)
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Var:
    label: str
    help: str
    scope: str
    example: str = ""


CATALOGUE = {
    "achieved": Var("Month achieved", "The station's sales for the month (AED), as on Targets.", "month",
                    "35000"),
    "target": Var("Month target", "The station's sales target for the month (AED).", "month", "30000"),
    "achievement_pct": Var("Achievement %", "achieved / target x 100.", "month", "116.67"),
    "work_days": Var("Working days", "Days with at least one person rostered Working at the station.", "month",
                     "28"),
    "basis": Var("Daily basis", "achieved / work_days: the same figure for every working day.", "month",
                 "1250"),
    "days_in_month": Var("Days in month", "Calendar days in the month.", "month", "30"),
    "cashiers": Var("Cashiers that day", "Cashiers rostered Working there that day (primary + secondary).",
                    "day", "1"),
    "labour": Var("Labour that day", "Labour rostered Working there that day.", "day", "2"),
    "tier2": Var("Tier 2 that day", "Secondary cashier + labour that day.", "day", "2"),
    "staff": Var("Staff that day", "Everyone rostered Working there that day.", "day", "3"),
    "role": Var("Role", 'The person\'s role that day: "primary", "secondary" or "labour".', "person",
                '"labour"'),
    "is_primary": Var("Is primary cashier", "1 for the primary cashier that day, else 0.", "person", "0"),
    "is_secondary": Var("Is secondary cashier", "1 for a secondary cashier that day, else 0.", "person", "0"),
    "is_labour": Var("Is labour", "1 for labour that day, else 0.", "person", "1"),
    "days_worked": Var("Days worked", "The person's working days at this station this month.", "person", "20"),
}

MONTH_NAMES = [name for name, var in CATALOGUE.items() if var.scope == "month"]
ROLES = ("primary", "secondary", "labour")


def as_list():
    """For the scheme page's panel and the check endpoint."""
    return [{"name": name, "label": v.label, "help": v.help, "scope": v.scope, "example": v.example}
            for name, v in CATALOGUE.items()]

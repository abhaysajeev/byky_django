"""The filter bar's dropdowns -- every list scoped to the user's company, each
item `{id, name, parent}` (`parent` drives the cascades: a station's state, a
vehicle type's category, a card grade's card type)."""

from apps.company.models import State
from apps.company.scoping import branches_for
from apps.discount.scoping import card_grades_for, card_types_for
from apps.fleet.scoping import asset_types_for, categories_for, vehicle_types_for
from apps.portal.scoping import users_for
from apps.reports.common import person

# Which dropdown cascades from which.
PARENT = {"branch": "state", "vehicle_type": "category", "card_grade": "card_type"}


def _items(rows):
    return sorted(rows, key=lambda r: (r["name"] or "").lower())


def options(user, keys):
    """{key: [items]} for the dropdown filters among `keys`."""
    out = {}
    branches = branches_for(user)
    if "state" in keys:
        out["state"] = _items({"id": s.pk, "name": s.name, "parent": None}
                              for s in State.objects.filter(locations__branches__in=branches).distinct())
    if "branch" in keys:
        out["branch"] = _items({"id": b.pk, "name": b.name, "parent": b.location.state_id}
                               for b in branches.select_related("location"))
    if "cashier" in keys:
        out["cashier"] = _items({"id": u.pk, "name": person(u), "parent": None}
                                for u in users_for(user).filter(employee__isnull=False).select_related("employee"))
    if "category" in keys:
        out["category"] = _items({"id": c.pk, "name": c.category_name, "parent": None} for c in categories_for(user))
    if "vehicle_type" in keys:
        out["vehicle_type"] = _items({"id": t.pk, "name": t.vehicle_type_name, "parent": t.category_id}
                                     for t in vehicle_types_for(user))
    if "card_type" in keys:
        out["card_type"] = _items({"id": t.pk, "name": t.name, "parent": None} for t in card_types_for(user))
    if "card_grade" in keys:
        out["card_grade"] = _items({"id": g.pk, "name": g.name, "parent": g.card_type_id}
                                   for g in card_grades_for(user))
    if "asset_type" in keys:
        out["asset_type"] = _items({"id": t.pk, "name": t.asset_type_name, "parent": None}
                                   for t in asset_types_for(user))
    return out


def names(opts):
    """{key: {id: name}} -- for the applied-filters line."""
    return {key: {item["id"]: item["name"] for item in items} for key, items in opts.items()}

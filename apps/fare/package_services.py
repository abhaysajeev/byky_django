"""Saving packages.

Views only translate HTTP; the rules live here. save_package() writes a whole
package -- its promotion items, free items and time slabs -- in one
transaction, after collecting every problem at once:

  - format problems (package_payload.parse);
  - scope: the company comes from the user, a branch or location must
    belong to it, and every vehicle type referenced by a row must too;
  - an item/free-item/time-slab id must belong to this very package.

No lock_version and no conflict/overlap checking -- see the note at the top
of the Package models in apps/fare/models.py for why: this pass has no
overlap invariant to enforce (unlike Fare), and there is nothing to
precedence-check until pricing/calculation is designed later.
"""

from django.db import IntegrityError, transaction
from django.db.models import Prefetch

from apps.company.scoping import branches_for, companies_for, locations_for
from apps.fare import package_payload
from apps.fare.models import (
    Fare,
    FareLevel,
    Package,
    PackageFreeItem,
    PackageFreeItemTimeSlab,
    PackageItem,
    PackageLevel,
)
from apps.fare.scoping import packages_for
from apps.fleet.scoping import vehicle_types_for
from core.enums import ApprovalStatus


class Invalid(Exception):
    def __init__(self, errors):
        super().__init__("; ".join(e["message"] for e in errors))
        self.errors = errors


class NotFound(Exception):
    pass


def _issues(issues):
    return [i.as_dict() for i in issues]


def _error(field, message, key="", missing=False):
    return {"key": key, "field": field, "message": message, "missing": missing}


def with_children(queryset):
    return queryset.select_related("company", "branch", "location").prefetch_related(
        Prefetch("items", queryset=PackageItem.objects.select_related("vehicle_type").order_by("pk")),
        Prefetch("free_items", queryset=PackageFreeItem.objects.select_related("vehicle_type").order_by("pk")),
        Prefetch("time_slabs", queryset=PackageFreeItemTimeSlab.objects.select_related("vehicle_type").order_by("pk")),
    )


# -- Who, what, where: the parts that depend on the user ------------------------------------


def _context(user, spec, meta, package=None):
    """Resolve company, branch and location within the user's scope. Returns
    (context, errors). A package being edited keeps its company."""
    errors = []
    if package is not None:
        company = package.company
    elif getattr(user, "sees_every_company", False):
        company = companies_for(user).filter(pk=meta["company"]).first()
        if company is None:
            errors.append(_error("Company", "Choose a company.", missing=True))
    else:
        company = user.company

    branch = None
    location = None
    if company is not None and spec is not None:
        if spec.level == PackageLevel.BRANCH and spec.branch:
            branch = branches_for(user).filter(pk=spec.branch, company=company).first()
            if branch is None:
                errors.append(_error("Branch", "Choose an active branch of this company."))
        elif spec.level == PackageLevel.LOCATION and spec.location:
            # Location carries no company_id -- locations_for(user) already
            # narrows it to zones this company has a branch in.
            location = locations_for(user).filter(pk=spec.location).first()
            if location is None:
                errors.append(_error("Location", "Choose a location this company has a branch in."))

    if company is not None and spec is not None:
        vehicle_type_ids = {row.vehicle_type for row in (*spec.items, *spec.free_items, *spec.time_slabs)}
        if vehicle_type_ids:
            known = set(vehicle_types_for(user).filter(pk__in=vehicle_type_ids, company=company)
                        .values_list("pk", flat=True))
            if vehicle_type_ids - known:
                errors.append(_error("Promotion Items", "Choose vehicle types that belong to this company."))

    return {"company": company, "branch": branch, "location": location}, errors


def _fares(company):
    """Active, approved fares of `company`, with their branch links."""
    return (Fare.objects.filter(company=company, is_active=True, approval_status=ApprovalStatus.APPROVED)
            .prefetch_related("branch_links__branch"))


def fare_choices(user):
    """Every fare a package row may pick, for the form's searchable picker:
    the vehicle type, the package minutes, the dates and the scope. The page
    narrows them to the package's company, scope and dates as it is filled."""
    fares = (Fare.objects.filter(company__in=companies_for(user), is_active=True,
                                 approval_status=ApprovalStatus.APPROVED)
             .select_related("vehicle_type").prefetch_related("branch_links__branch")
             .order_by("vehicle_type__vehicle_type_name", "package_minutes", "valid_from"))
    return [{
        "id": f.pk, "company": f.company_id, "vehicle_type": f.vehicle_type_id,
        "vehicle_type_name": f.vehicle_type.vehicle_type_name, "package_minutes": f.package_minutes,
        "valid_from": f.valid_from.isoformat(), "valid_to": f.valid_to.isoformat(), "level": f.level,
        "branches": sorted(link.branch_id for link in f.branch_links.all()),
        "locations": sorted({link.branch.location_id for link in f.branch_links.all()}),
    } for f in fares]


def fits_scope(fare, level, branch, location):
    """Whether a fare can price rentals under a package of this scope: a
    company-level fare always; a branch fare when the package is the
    company's, or the fare covers the package's branch / a branch in its
    location."""
    if fare.level == FareLevel.COMPANY or level == PackageLevel.COMPANY:
        return True
    links = list(fare.branch_links.all())
    if level == PackageLevel.BRANCH:
        return branch is not None and any(link.branch_id == branch.pk for link in links)
    return location is not None and any(link.branch.location_id == location.pk for link in links)


def _fare_errors(spec, context):
    """Every row's vehicle type and package minutes must be an existing fare
    that is in force within the package's dates and fits its scope -- package
    minutes are picked from fares, never typed (owner, 26 Nov 2026)."""
    company = context["company"]
    if company is None or spec is None:
        return []
    fares = [f for f in _fares(company).filter(valid_from__lte=spec.valid_to, valid_to__gte=spec.valid_from)
             if fits_scope(f, spec.level, context["branch"], context["location"])]
    known = {(f.vehicle_type_id, f.package_minutes) for f in fares}
    errors = []
    for label, rows in (("Promotion Items", spec.items), ("Free Promotion Items", spec.free_items),
                        ("Free Item Time Slabs", spec.time_slabs)):
        for row in rows:
            if (row.vehicle_type, row.package_minutes) not in known:
                errors.append(_error(label, f"{row.package_minutes} min has no active fare for this vehicle type "
                                            "in the package's dates and scope. Pick one from the fare list.",
                                     key=row.key))
    return errors


def _own_ids(meta, package):
    """An item/free-item/time-slab id must belong to the package being saved --
    never a way to reach into another package's rows."""
    item_ids = set(package.items.values_list("pk", flat=True)) if package else set()
    free_ids = set(package.free_items.values_list("pk", flat=True)) if package else set()
    slab_ids = set(package.time_slabs.values_list("pk", flat=True)) if package else set()
    if (any(i and i not in item_ids for i in meta["item_ids"].values())
            or any(i and i not in free_ids for i in meta["free_item_ids"].values())
            or any(i and i not in slab_ids for i in meta["time_slab_ids"].values())):
        return [_error("Promotion Items", "Some rows could not be matched to this package. Reload and try again.")]
    return []


# -- Save -----------------------------------------------------------------------------------


CONSTRAINT_MESSAGES = {
    "uniq_package_code_per_company": ("Package Code", "Another package already uses this code."),
}


def _friendly(error):
    name = getattr(getattr(error.__cause__, "diag", None), "constraint_name", "") or ""
    field, message = CONSTRAINT_MESSAGES.get(
        name, ("Package", "This package conflicts with another change. Reload and try again."))
    return [_error(field, message)]


def save_package(user, data):
    """Create or update a whole package. Returns the saved Package; raises
    Invalid or NotFound."""
    parsed = package_payload.parse(data)
    meta = parsed.meta
    try:
        with transaction.atomic():
            package = None
            if meta["pk"]:
                package = packages_for(user).filter(pk=meta["pk"]).first()
                if package is None:
                    raise NotFound()

            spec = parsed.spec
            context, scope_errors = _context(user, spec, meta, package)
            errors = _issues(parsed.errors) + scope_errors + (_own_ids(meta, package) if spec else [])
            if not scope_errors:
                errors += _fare_errors(spec, context)
            if errors:
                raise Invalid(errors)

            package = _write(user, package, context, spec, meta)
    except IntegrityError as error:
        raise Invalid(_friendly(error)) from error

    return with_children(Package.objects.filter(pk=package.pk)).get()


def _write(user, package, context, spec, meta):
    keep_items = {i for i in meta["item_ids"].values() if i}
    keep_free = {i for i in meta["free_item_ids"].values() if i}
    keep_slabs = {i for i in meta["time_slab_ids"].values() if i}

    if package is not None:
        package.items.exclude(pk__in=keep_items).delete()
        package.free_items.exclude(pk__in=keep_free).delete()
        package.time_slabs.exclude(pk__in=keep_slabs).delete()
    else:
        # Approval: same as every other master for now -- saved approved,
        # with the Active / Inactive the form chose.
        package = Package(company=context["company"], created_by=user,
                     want_approval=False, approval_status=ApprovalStatus.APPROVED)

    package.package_code = spec.package_code
    package.package_name = spec.package_name
    package.level = spec.level
    package.branch = context["branch"]
    package.location = context["location"]
    package.valid_from, package.valid_to = spec.valid_from, spec.valid_to
    package.promotion_for = spec.promotion_for
    package.lower_value, package.upper_value = spec.lower_value, spec.upper_value
    package.promotion_type = spec.promotion_type
    package.time_slab_applicable = spec.time_slab_applicable
    package.free_item_selectable = spec.free_item_selectable
    package.free_item_selectable_note = spec.free_item_selectable_note
    package.free_or_package_price = spec.free_or_package_price
    package.include_regular_discounts = spec.include_regular_discounts
    package.is_active = meta["is_active"]
    package.modified_by = user
    package.save()

    existing_items = {r.pk: r for r in package.items.all()}
    for row in spec.items:
        item = existing_items.get(meta["item_ids"].get(row.key)) or PackageItem(package=package)
        item.vehicle_type_id = row.vehicle_type
        item.package_minutes = row.package_minutes
        item.value = row.value
        item.save()

    existing_free = {r.pk: r for r in package.free_items.all()}
    for row in spec.free_items:
        free_item = existing_free.get(meta["free_item_ids"].get(row.key)) or PackageFreeItem(package=package)
        free_item.vehicle_type_id = row.vehicle_type
        free_item.package_minutes = row.package_minutes
        free_item.value = row.value
        free_item.save()

    existing_slabs = {r.pk: r for r in package.time_slabs.all()}
    for row in spec.time_slabs:
        slab = existing_slabs.get(meta["time_slab_ids"].get(row.key)) or PackageFreeItemTimeSlab(package=package)
        slab.date_mode = row.date_mode
        slab.specific_date = row.specific_date
        slab.day = row.day
        slab.from_time = row.start
        slab.to_time = row.end
        slab.vehicle_type_id = row.vehicle_type
        slab.package_minutes = row.package_minutes
        slab.value = row.value
        slab.save()

    return package

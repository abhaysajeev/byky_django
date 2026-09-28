"""Saving offers.

Views only translate HTTP; the rules live here. save_offer() writes a whole
offer -- its promotion items, free items and time slabs -- in one
transaction, after collecting every problem at once:

  - format problems (offer_payload.parse);
  - scope: the company comes from the user, a branch or location must
    belong to it, and every vehicle type referenced by a row must too;
  - an item/free-item/time-slab id must belong to this very offer.

No lock_version and no conflict/overlap checking -- see the note at the top
of the Offer models in apps/fare/models.py for why: this pass has no
overlap invariant to enforce (unlike Fare), and there is nothing to
precedence-check until pricing/calculation is designed later.
"""

from django.db import IntegrityError, transaction
from django.db.models import Prefetch

from apps.company.scoping import branches_for, companies_for, locations_for
from apps.fare import offer_payload
from apps.fare.models import Offer, OfferFreeItem, OfferFreeItemTimeSlab, OfferItem, OfferLevel
from apps.fare.scoping import offers_for
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
        Prefetch("items", queryset=OfferItem.objects.select_related("vehicle_type").order_by("pk")),
        Prefetch("free_items", queryset=OfferFreeItem.objects.select_related("vehicle_type").order_by("pk")),
        Prefetch("time_slabs", queryset=OfferFreeItemTimeSlab.objects.select_related("vehicle_type").order_by("pk")),
    )


# -- Who, what, where: the parts that depend on the user ------------------------------------


def _context(user, spec, meta, offer=None):
    """Resolve company, branch and location within the user's scope. Returns
    (context, errors). An offer being edited keeps its company."""
    errors = []
    if offer is not None:
        company = offer.company
    elif getattr(user, "sees_every_company", False):
        company = companies_for(user).filter(pk=meta["company"]).first()
        if company is None:
            errors.append(_error("Company", "Choose a company.", missing=True))
    else:
        company = user.company

    branch = None
    location = None
    if company is not None and spec is not None:
        if spec.level == OfferLevel.BRANCH and spec.branch:
            branch = branches_for(user).filter(pk=spec.branch, company=company).first()
            if branch is None:
                errors.append(_error("Branch", "Choose an active branch of this company."))
        elif spec.level == OfferLevel.LOCATION and spec.location:
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


def _own_ids(meta, offer):
    """An item/free-item/time-slab id must belong to the offer being saved --
    never a way to reach into another offer's rows."""
    item_ids = set(offer.items.values_list("pk", flat=True)) if offer else set()
    free_ids = set(offer.free_items.values_list("pk", flat=True)) if offer else set()
    slab_ids = set(offer.time_slabs.values_list("pk", flat=True)) if offer else set()
    if (any(i and i not in item_ids for i in meta["item_ids"].values())
            or any(i and i not in free_ids for i in meta["free_item_ids"].values())
            or any(i and i not in slab_ids for i in meta["time_slab_ids"].values())):
        return [_error("Promotion Items", "Some rows could not be matched to this offer. Reload and try again.")]
    return []


# -- Save -----------------------------------------------------------------------------------


CONSTRAINT_MESSAGES = {
    "uniq_offer_code_per_company": ("Promotion Code", "Another offer already uses this code."),
}


def _friendly(error):
    name = getattr(getattr(error.__cause__, "diag", None), "constraint_name", "") or ""
    field, message = CONSTRAINT_MESSAGES.get(
        name, ("Offer", "This offer conflicts with another change. Reload and try again."))
    return [_error(field, message)]


def save_offer(user, data):
    """Create or update a whole offer. Returns the saved Offer; raises
    Invalid or NotFound."""
    parsed = offer_payload.parse(data)
    meta = parsed.meta
    try:
        with transaction.atomic():
            offer = None
            if meta["pk"]:
                offer = offers_for(user).filter(pk=meta["pk"]).first()
                if offer is None:
                    raise NotFound()

            spec = parsed.spec
            context, scope_errors = _context(user, spec, meta, offer)
            errors = _issues(parsed.errors) + scope_errors + (_own_ids(meta, offer) if spec else [])
            if errors:
                raise Invalid(errors)

            offer = _write(user, offer, context, spec, meta)
    except IntegrityError as error:
        raise Invalid(_friendly(error)) from error

    return with_children(Offer.objects.filter(pk=offer.pk)).get()


def _write(user, offer, context, spec, meta):
    keep_items = {i for i in meta["item_ids"].values() if i}
    keep_free = {i for i in meta["free_item_ids"].values() if i}
    keep_slabs = {i for i in meta["time_slab_ids"].values() if i}

    if offer is not None:
        offer.items.exclude(pk__in=keep_items).delete()
        offer.free_items.exclude(pk__in=keep_free).delete()
        offer.time_slabs.exclude(pk__in=keep_slabs).delete()
    else:
        # Approval: same as every other master for now -- saved approved,
        # with the Active / Inactive the form chose.
        offer = Offer(company=context["company"], created_by=user,
                     want_approval=False, approval_status=ApprovalStatus.APPROVED)

    offer.offer_code = spec.offer_code
    offer.offer_name = spec.offer_name
    offer.level = spec.level
    offer.branch = context["branch"]
    offer.location = context["location"]
    offer.valid_from, offer.valid_to = spec.valid_from, spec.valid_to
    offer.promotion_for = spec.promotion_for
    offer.inventory_type = spec.inventory_type
    offer.lower_value, offer.upper_value = spec.lower_value, spec.upper_value
    offer.promotion_type = spec.promotion_type
    offer.time_slab_applicable = spec.time_slab_applicable
    offer.free_item_selectable = spec.free_item_selectable
    offer.free_item_selectable_note = spec.free_item_selectable_note
    offer.free_or_offer_price = spec.free_or_offer_price
    offer.is_active = meta["is_active"]
    offer.modified_by = user
    offer.save()

    existing_items = {r.pk: r for r in offer.items.all()}
    for row in spec.items:
        item = existing_items.get(meta["item_ids"].get(row.key)) or OfferItem(offer=offer)
        item.vehicle_type_id = row.vehicle_type
        item.package_minutes = row.package_minutes
        item.value = row.value
        item.save()

    existing_free = {r.pk: r for r in offer.free_items.all()}
    for row in spec.free_items:
        free_item = existing_free.get(meta["free_item_ids"].get(row.key)) or OfferFreeItem(offer=offer)
        free_item.vehicle_type_id = row.vehicle_type
        free_item.package_minutes = row.package_minutes
        free_item.value = row.value
        free_item.save()

    existing_slabs = {r.pk: r for r in offer.time_slabs.all()}
    for row in spec.time_slabs:
        slab = existing_slabs.get(meta["time_slab_ids"].get(row.key)) or OfferFreeItemTimeSlab(offer=offer)
        slab.date_mode = row.date_mode
        slab.specific_date = row.specific_date
        slab.day = row.day
        slab.from_time = row.start
        slab.to_time = row.end
        slab.vehicle_type_id = row.vehicle_type
        slab.package_minutes = row.package_minutes
        slab.value = row.value
        slab.save()

    return offer

"""Rental Management services."""

from django.db import IntegrityError, transaction

from apps.rental.models import Customer, full_number


def next_customer_code(company):
    """CU001, CU002, ... -- the next unused code for this company.

    Not exposed on the Add form: the wireframe's own Add form has no
    Customer Code field, only its list column does, so it's generated here.
    """
    last = (
        Customer.objects.filter(company=company, customer_code__startswith="CU")
        .order_by("-customer_code")
        .values_list("customer_code", flat=True)
        .first()
    )
    next_number = int(last[2:]) + 1 if last and last[2:].isdigit() else 1
    code = f"CU{next_number:03d}"
    while Customer.objects.filter(company=company, customer_code=code).exists():
        next_number += 1
        code = f"CU{next_number:03d}"
    return code


class UnknownCustomer(Exception):
    pass


def customer_by_phone(company, full_number):
    """The device lookup, by the full number (country code + number, digits
    only) -- one index hit. Filtered by company too, even though the full
    number is globally unique, as a defense-in-depth company boundary: a
    device at one company never fetches another company's customer, even by
    guessing a phone number that happens to be theirs."""
    customer = Customer.objects.filter(company=company, mobile_full=full_number).first()
    if customer is None:
        raise UnknownCustomer()
    return customer


class CustomerExists(Exception):
    """The phone is already registered. `customer` is the existing one when it
    belongs to the caller's company, None when it belongs to another -- whose
    details are never handed across."""

    def __init__(self, customer):
        super().__init__("customer_exists")
        self.customer = customer


class SyncIdConflict(Exception):
    """This sync_id is already another company's customer."""


# What a device may set on a new customer. Blocking, the code and the
# document upload stay with the web screen.
_DEVICE_FIELDS = (
    "first_name", "last_name", "gender", "date_of_birth", "nationality", "id_type", "id_no",
    "mobile_country_code", "mobile_no", "email", "address", "remarks",
)


def create_customer(user, company, values):
    """A customer registered by the operator app. Returns (customer, created):
    created is False when this sync_id was already stored, which the app
    treats as success -- an offline create resent is stored once.

    Raises CustomerExists or SyncIdConflict; nothing is written then.
    """
    sync_id = values["sync_id"]
    existing = _by_sync_id(company, sync_id)
    if existing is not None:
        return existing, False
    _refuse_taken_phone(company, full_number(values["mobile_country_code"], values["mobile_no"]))

    customer = Customer(company=company, sync_id=sync_id, created_by=user, modified_by=user,
                        **{field: values[field] for field in _DEVICE_FIELDS if field in values})
    customer.apply_approval_defaults()
    try:
        with transaction.atomic():
            customer.customer_code = next_customer_code(company)
            customer.save(force_insert=True)
    except IntegrityError:
        # Lost a race: the same sync_id or the same phone arrived at once.
        existing = _by_sync_id(company, sync_id)
        if existing is not None:
            return existing, False
        _refuse_taken_phone(company, customer.mobile_full)
        raise
    return customer, True


def _by_sync_id(company, sync_id):
    existing = Customer.objects.filter(sync_id=sync_id).first()
    if existing is not None and existing.company_id != company.pk:
        raise SyncIdConflict()
    return existing


def _refuse_taken_phone(company, mobile_full):
    taken = Customer.objects.filter(mobile_full=mobile_full).first()
    if taken is not None:
        raise CustomerExists(taken if taken.company_id == company.pk else None)

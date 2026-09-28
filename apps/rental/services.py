"""Rental Management services."""

from apps.rental.models import Customer


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


def customer_by_phone(company, mobile_no):
    """The device lookup: filtered by company too, even though mobile_no is
    globally unique, as a defense-in-depth company boundary -- a device at
    one company should never be able to fetch another company's customer,
    even by guessing a phone number that happens to be theirs."""
    customer = Customer.objects.filter(company=company, mobile_no=mobile_no).first()
    if customer is None:
        raise UnknownCustomer()
    return customer

"""The database's own guard on the customer table: company+code uniqueness,
the full number's global (not per-company) uniqueness, and sync_id."""

import uuid

import pytest
from django.db import IntegrityError, transaction

from apps.rental.models import full_number
from apps.rental.tests.conftest import make_customer


def refused(fn, constraint=None):
    with pytest.raises(IntegrityError) as caught, transaction.atomic():
        fn()
    if constraint:
        assert caught.value.__cause__.diag.constraint_name == constraint


def test_customer_code_is_unique_per_company(world):
    make_customer(world, customer_code="CU001", mobile_no="501111111")
    refused(lambda: make_customer(world, customer_code="CU001", mobile_no="501111112"),
            "uniq_customer_code_per_company")
    # Another company may reuse the same code.
    make_customer(world, company=world["other"], customer_code="CU001", mobile_no="501111113")


def test_the_full_number_is_country_code_and_number_digits_only():
    assert full_number("+971", "50 123-4567") == "971501234567"
    assert full_number("+91", "7025985344") == "917025985344"


def test_saving_fills_the_full_number(world):
    customer = make_customer(world, mobile_country_code="+971", mobile_no="50 123 4567")
    assert customer.mobile_full == "971501234567"
    customer.mobile_country_code = "+91"
    customer.save(update_fields=["mobile_country_code"])
    customer.refresh_from_db()
    assert customer.mobile_full == "91501234567"


def test_the_full_number_is_globally_unique_not_per_company(world):
    make_customer(world, mobile_no="501111111")
    refused(lambda: make_customer(world, customer_code="CU002", mobile_no="501111111"),
            "uniq_customer_mobile_full")
    # Even across companies: one phone is one person.
    refused(
        lambda: make_customer(world, company=world["other"], customer_code="CU001", mobile_no="501111111"),
        "uniq_customer_mobile_full",
    )
    # The same number typed differently is still the same phone.
    refused(lambda: make_customer(world, customer_code="CU003", mobile_no="50 111 1111"),
            "uniq_customer_mobile_full")


def test_the_same_local_number_under_two_country_codes_is_two_customers(world):
    make_customer(world, mobile_country_code="+971", mobile_no="7025985344")
    make_customer(world, customer_code="CU002", mobile_country_code="+91", mobile_no="7025985344")


def test_web_customers_have_no_sync_id_and_never_collide(world):
    make_customer(world, mobile_no="501111111")
    make_customer(world, customer_code="CU002", mobile_no="501111112")
    shared = uuid.uuid4()
    make_customer(world, customer_code="CU003", mobile_no="501111113", sync_id=shared)
    refused(lambda: make_customer(world, customer_code="CU004", mobile_no="501111114", sync_id=shared))

"""The database's own guard on the customer table: company+code uniqueness,
and mobile_no's global (not per-company) uniqueness."""

import pytest
from django.db import IntegrityError, transaction

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


def test_mobile_no_is_globally_unique_not_per_company(world):
    make_customer(world, mobile_no="501111111")
    refused(lambda: make_customer(world, customer_code="CU002", mobile_no="501111111"),
            "uniq_customer_mobile_no")
    # Even across companies -- this is the one place this model's own
    # constraints deliberately differ from every other *_code precedent.
    refused(
        lambda: make_customer(world, company=world["other"], customer_code="CU001", mobile_no="501111111"),
        "uniq_customer_mobile_no",
    )

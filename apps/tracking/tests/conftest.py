"""Tracking tests reuse the rental tests' small world: two companies with a
station each, and a signed-in administrator."""

import pytest
from django.db import connection

from apps.rental.tests.conftest import client_in, user, world  # noqa: F401


@pytest.fixture(autouse=True)
def _empty_readings(request):
    """tracking_reading is not a Django-managed table (partitioned, RunSQL), so
    the flush after a transactional test leaves its rows: empty it around every
    test that uses the database."""
    marker = request.node.get_closest_marker("django_db")
    if marker is None:
        yield
        return
    request.getfixturevalue("transactional_db" if marker.kwargs.get("transaction") else "db")
    with connection.cursor() as cursor:
        cursor.execute("TRUNCATE tracking_reading")
    yield
    with connection.cursor() as cursor:
        cursor.execute("TRUNCATE tracking_reading")

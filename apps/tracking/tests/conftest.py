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
    _restore_pages()
    with connection.cursor() as cursor:
        cursor.execute("TRUNCATE tracking_reading")
    yield
    with connection.cursor() as cursor:
        cursor.execute("TRUNCATE tracking_reading")


def _restore_pages():
    """The flush after a transactional test also empties the page registry the
    migrations wrote, so a later test on that worker would find no tracking
    pages (and its admin no rights on them). Write it back when it is gone."""
    from apps.portal.models import Page
    from apps.portal.page_sync import sync

    if not Page.objects.exists():
        sync()

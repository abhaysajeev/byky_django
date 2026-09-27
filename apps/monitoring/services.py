"""Retention: rows older than their keep-for are deleted in batches.

Batches keep each DELETE short, so the writer's inserts never queue behind one
long lock. Run by the writer thread every MONITORING_PURGE_HOURS and by
`manage.py purge_logs`.
"""

import datetime

from django.conf import settings
from django.db import connection
from django.utils import timezone

from apps.monitoring.models import ErrorLog, RequestLog

BATCH = 5_000
# Any constant: two processes asking for the same lock number cannot both hold it.
PURGE_LOCK = 0x4259_4B59_4C4F_47       # "BYKYLOG"


def cutoffs(now=None):
    now = now or timezone.now()
    return {
        RequestLog: now - datetime.timedelta(days=settings.MONITORING_REQUEST_DAYS),
        ErrorLog: now - datetime.timedelta(days=settings.MONITORING_ERROR_DAYS),
    }


def purge(*, now=None, lock=False):
    """{model name: rows deleted}. With lock=True, does nothing if another
    process is already purging."""
    if lock:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [PURGE_LOCK])
            if not cursor.fetchone()[0]:
                return {}
    try:
        deleted = {}
        for model, before in cutoffs(now).items():
            total = 0
            while True:
                ids = list(model.objects.filter(created_at__lt=before).values_list("pk", flat=True)[:BATCH])
                if not ids:
                    break
                total += model.objects.filter(pk__in=ids).delete()[0]
            deleted[model.__name__] = total
        return deleted
    finally:
        if lock:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [PURGE_LOCK])

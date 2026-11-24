"""Crew jobs for the Celery worker (config/celery.py)."""

from celery import shared_task

from apps.crew import services


@shared_task(name="apps.crew.tasks.auto_close_due", ignore_result=True)
def auto_close_due():
    """Auto-close the punch-ins nobody closed once their business day is over
    (owner, 24 Nov 2026). Idempotent: a later run changes nothing."""
    return services.auto_close_due()

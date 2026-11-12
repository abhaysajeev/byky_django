"""Targets jobs for the Celery worker (config/celery.py)."""

from celery import shared_task

from apps.targets import services


@shared_task(name="apps.targets.tasks.freeze_due", ignore_result=True)
def freeze_due():
    """Freeze each target's required amount for the business day just started
    (design/08-targets.md T5). Idempotent: a later run changes nothing."""
    return services.freeze_due()

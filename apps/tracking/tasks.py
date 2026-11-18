"""Tracking jobs for the Celery worker (config/celery.py)."""

from celery import shared_task

from apps.tracking import alerts


@shared_task(name="apps.tracking.tasks.manager_alert_push", ignore_result=True)
def manager_alert_push(found, at):
    """Tell the managers what this cycle found (apps/tracking/alerts.py)."""
    return alerts.push(found, at)

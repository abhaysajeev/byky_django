"""Crew jobs for the Celery worker (config/celery.py)."""

from celery import shared_task

from apps.crew import services


@shared_task(name="apps.crew.tasks.auto_close_due", ignore_result=True)
def auto_close_due():
    """Auto-close the punch-ins nobody closed once their business day is over
    (owner, 24 Nov 2026). Idempotent: a later run changes nothing."""
    return services.auto_close_due()


@shared_task(name="apps.crew.tasks.punch_out_reminders_due", ignore_result=True)
def punch_out_reminders_due():
    """Remind on-duty employees still punched in 15 minutes after their
    rostered shift ended (owner, 26 Nov 2026). Once per punch-in."""
    return services.punch_out_reminders_due()

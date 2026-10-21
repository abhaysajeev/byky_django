"""Notification jobs for the Celery worker (config/celery.py); the schedule is
CELERY_BEAT_SCHEDULE in config/settings.py."""

from celery import shared_task

from apps.notifications import services


@shared_task(name="apps.notifications.tasks.send")
def send(ids):
    """Send these rows; any to retry are queued again after their wait."""
    for notification_id in ids:
        retry_in = services.send_one(notification_id)
        if retry_in is not None:
            services.queue([notification_id], countdown=retry_in)


@shared_task(name="apps.notifications.tasks.send_due")
def send_due():
    """Every minute: rows the change could not queue (Redis down) or whose
    retry is due."""
    for notification_id in services.due():
        services.send_one(notification_id)


@shared_task(name="apps.notifications.tasks.purge")
def purge():
    return services.purge()

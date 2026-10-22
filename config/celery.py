"""Celery: the background jobs (worker) and the schedule (beat).

Settings come from Django's under the CELERY_ prefix (config/settings.py);
tasks are found in each app's tasks.py. Each deployment runs its own Redis,
worker and beat (docker-compose.qa.yml), so qa and dev never share a queue.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("byky")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

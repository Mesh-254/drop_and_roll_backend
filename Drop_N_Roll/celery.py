import os
from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "Drop_N_Roll.settings")
app = Celery("Drop_N_Roll")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


app.conf.beat_schedule = {
    'optimize-bookings-every-15-min': {
        'task': 'bookings.tasks.optimize_bookings',
        'schedule': crontab(hour='*/3'),  # Every 3 hours
    },
    'mark-overdue-shifts': {
        'task': 'bookings.tasks.mark_overdue_shifts',
        'schedule': crontab(minute=0, hour=0),  # Daily at midnight
    },
}
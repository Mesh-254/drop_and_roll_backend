import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "Drop_N_Roll.settings")
app = Celery("Drop_N_Roll")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
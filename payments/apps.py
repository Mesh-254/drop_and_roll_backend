from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'payments'

    def ready(self):
        # Import signals here—lazy, post-models
        import payments.signals  # This connects the receivers

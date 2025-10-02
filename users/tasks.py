from celery import shared_task
from django.core.mail import send_mail


@shared_task
def send_confirmation_email(subject, message, from_email, recipient_list):
    send_mail(subject, message, from_email, recipient_list)


@shared_task
def send_reset_email(subject, message, from_email, recipient_list):
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=from_email,
            recipient_list=recipient_list,
            fail_silently=False,  # Raise on failure for retry
        )

    except Exception as e:
        # Celery will retry (configure in settings: task_acks_late=True)
        raise  # Re-raise for Celery retry


@shared_task
def send_welcome_email(subject, message, from_email, recipient_list):
    """
    Sends a welcome email after account confirmation.
    Retries on failure via Celery configuration.
    """
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=from_email,
            recipient_list=recipient_list,
            fail_silently=False,  # Raise on failure for retry
        )
    except Exception as e:
        # Log for monitoring (integrate with your logger in production)
        print(f"Welcome email failed for {recipient_list}: {str(e)}")  # Replace with logger.error()
        raise  # Re-raise for Celery retry
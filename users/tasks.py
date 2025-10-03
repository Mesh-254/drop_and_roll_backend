from celery import shared_task
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.conf import settings
import logging

logger = logging.getLogger(__name__)

@shared_task(bind=True, max_retries=3)
def send_confirmation_email(self, subject, context, from_email, recipient_list):
    """Send confirmation email with styled template."""
    try:
        html_message = render_to_string('emails/user_confirmation.html', context)
        plain_message = strip_tags(html_message)
        send_mail(
            subject=subject,
            message=plain_message,
            from_email=from_email,
            recipient_list=recipient_list,
            html_message=html_message,
            fail_silently=False,
        )
        logger.info(f"Confirmation email sent to {recipient_list}")
    except Exception as e:
        logger.error(f"Confirmation email failed for {recipient_list}: {str(e)}")
        raise self.retry(exc=e, countdown=60 * (2 ** self.request.retries))  # Exponential backoff

@shared_task(bind=True, max_retries=3)
def send_reset_email(self, subject, context, from_email, recipient_list):
    """Send password reset email with styled template."""
    try:
        html_message = render_to_string('emails/user_password_reset.html', context)
        plain_message = strip_tags(html_message)
        send_mail(
            subject=subject,
            message=plain_message,
            from_email=from_email,
            recipient_list=recipient_list,
            html_message=html_message,
            fail_silently=False,
        )
        logger.info(f"Reset email sent to {recipient_list}")
    except Exception as e:
        logger.error(f"Reset email failed for {recipient_list}: {str(e)}")
        raise self.retry(exc=e, countdown=60 * (2 ** self.request.retries))

@shared_task(bind=True, max_retries=3)
def send_welcome_email(self, subject, context, from_email, recipient_list):
    """Send welcome email after activation with styled template."""
    try:
        html_message = render_to_string('emails/user_welcome.html', context)
        plain_message = strip_tags(html_message)
        send_mail(
            subject=subject,
            message=plain_message,
            from_email=from_email,
            recipient_list=recipient_list,
            html_message=html_message,
            fail_silently=False,
        )
        logger.info(f"Welcome email sent to {recipient_list}")
    except Exception as e:
        logger.error(f"Welcome email failed for {recipient_list}: {str(e)}")
        raise self.retry(exc=e, countdown=60 * (2 ** self.request.retries))
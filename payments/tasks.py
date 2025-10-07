# payments/tasks.py
import logging
from celery import shared_task
from celery.exceptions import Retry
from django.core.mail import EmailMultiAlternatives
from django.conf import settings
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.core.exceptions import ValidationError  # For email validation

logger = logging.getLogger(__name__)  # Django's logging setup (configure in settings.py)

@shared_task(bind=True, max_retries=3, default_retry_delay=60)  # Bind for self-retry; retry up to 3x with 1-min delay
def send_refund_notification_email(self, booking_id, amount, reason, email, currency='KES'):
    # Input validation (prevents bad data from wasting queue space)
    if not email or '@' not in email:
        logger.error(f"Invalid email '{email}' for refund notification (booking {booking_id})")
        return False  # Or raise Ignore() to drop the task silently

    subject = f'Refund Processed for Your Booking #{booking_id}'

    try:
        # Render HTML template
        html_message = render_to_string('emails/refund_notification.html', {
            'booking_id': booking_id,
            'amount': amount,
            'currency': currency,
            'reason': reason,
        })
    except Exception as e:
        logger.error(f"Template rendering failed for booking {booking_id}: {str(e)}")
        raise Retry(self.retry(countdown=30))  # Retry template issues

    # Use stripped HTML as plain text body (more reliable than hardcoded fallback)
    text_body = strip_tags(html_message)

    # Build and send email
    email_msg = EmailMultiAlternatives(
        subject,
        text_body,
        settings.DEFAULT_FROM_EMAIL,
        [email],
        headers={'X-Refund-Booking-ID': str(booking_id)}  # Optional: Custom header for tracking
    )
    email_msg.attach_alternative(html_message, "text/html")

    try:
        email_msg.send(fail_silently=False)
        logger.info(f"Refund email sent successfully to {email} for booking {booking_id} ({amount} {currency})")
        return True
    except Exception as e:
        logger.error(f"Failed to send refund email to {email} for booking {booking_id}: {str(e)}")
        # Retry on transient errors (e.g., SMTP timeout); otherwise, let it fail
        raise self.retry(countdown=60 * (self.request.retries + 1))  # Exponential backoff
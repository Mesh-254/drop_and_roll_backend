from celery import shared_task
from django.core.mail import send_mail
from django.conf import settings
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from .models import Booking
from payments.models import PaymentTransaction

@shared_task
def send_booking_confirmation_email(subject, message, from_email, recipient_list):
    send_mail(subject, message, from_email, recipient_list)

@shared_task
def send_reminder(subject, message, from_email, recipient_list):
    send_mail(subject, message, from_email, recipient_list)

@shared_task
def send_booking_payment_success_email(booking_id, recipient_email):
    """Send combined success email: Booking confirmed + payment succeeded."""
    try:
        booking = Booking.objects.select_related('quote', 'customer').get(id=booking_id)
        payment = PaymentTransaction.objects.get(booking=booking)  # Assume 1:1; adjust if needed
        if payment.status != 'success':  # PaymentStatus.SUCCESS
            return  # Bail if not success

        subject = f' Payment Successful: Booking #{booking.id} Confirmed ! '
        context = {
            'booking': booking,
            'payment': payment,
            'site_name': 'Drop \'n Roll',
            'support_email': settings.DEFAULT_FROM_EMAIL,
        }
        html_message = render_to_string('emails/booking_payment_success.html', context)
        plain_message = strip_tags(html_message)

        send_mail(
            subject=subject,
            message=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient_email],
            html_message=html_message,
            fail_silently=False,
        )
    except (Booking.DoesNotExist, PaymentTransaction.DoesNotExist):
        pass  # Log if needed: logger.error(f"Booking/Payment {booking_id} not found")

@shared_task
def send_booking_payment_failure_email(booking_id, recipient_email, failure_reason='Payment did not succeed'):
    """Send combined failure email: Booking details + payment failed."""
    try:
        booking = Booking.objects.select_related('quote', 'customer').get(id=booking_id)
        payment = PaymentTransaction.objects.get(booking=booking)
        if payment.status != 'failed':  # PaymentStatus.FAILED
            return  # Bail if not failure

        subject = f'Booking #{booking.id} – Payment Failed: Action Required'
        context = {
            'booking': booking,
            'payment': payment,
            'failure_reason': failure_reason,
            'site_name': 'Drop \'n Roll',
            'support_email': settings.DEFAULT_FROM_EMAIL,
            'new_booking_url': f"{settings.FRONTEND_URL}/booking",  # Adjust to your frontend route
        }
        html_message = render_to_string('emails/booking_payment_failure.html', context)
        plain_message = strip_tags(html_message)

        send_mail(
            subject=subject,
            message=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient_email],
            html_message=html_message,
            fail_silently=False,
        )
    except (Booking.DoesNotExist, PaymentTransaction.DoesNotExist):
        pass  # Log if needed

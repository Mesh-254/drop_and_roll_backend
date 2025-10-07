# payments/signals.py
from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from django.utils import timezone
import logging

from bookings.tasks import send_booking_payment_success_email, send_booking_payment_failure_email
from .models import PaymentTransaction, PaymentStatus
from .models import Refund
from .tasks import send_refund_notification_email

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=PaymentTransaction)
def capture_old_status(sender, instance, **kwargs):
    """
    Capture the old status before save to detect changes in post_save.
    """
    if instance.pk:
        try:
            old_instance = sender.objects.get(pk=instance.pk)
            instance._old_status = old_instance.status
        except sender.DoesNotExist:
            instance._old_status = None
    else:
        instance._old_status = None


@receiver(post_save, sender=PaymentTransaction)
def handle_payment_status_change(sender, instance, created, **kwargs):
    """
    Trigger emails on status changes to SUCCESS or FAILED for bookings.
    Only fires if status actually changed and there's a related booking.
    """
    if created:
        return  # No email on creation (status is PENDING)

    old_status = getattr(instance, '_old_status', None)
    if old_status == instance.status:
        return  # No change

    # SUCCESS transition
    if instance.status == PaymentStatus.SUCCESS and old_status != PaymentStatus.SUCCESS:
        if instance.booking:
            recipient_email = (
                instance.booking.customer.email if instance.booking.customer
                else instance.booking.guest_email
            )
            if recipient_email:
                try:
                    send_booking_payment_success_email.delay(
                        instance.booking.id, recipient_email
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to queue success email for tx {instance.id}: {str(e)}"
                    )

    # FAILED transition
    elif instance.status == PaymentStatus.FAILED and old_status != PaymentStatus.FAILED:
        if instance.booking:
            recipient_email = (
                instance.booking.customer.email if instance.booking.customer
                else instance.booking.guest_email
            )
            if recipient_email:
                try:
                    send_booking_payment_failure_email.delay(
                        instance.booking.id, recipient_email, 'Payment did not succeed'
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to queue failure email for tx {instance.id}: {str(e)}"
                    )


@receiver(pre_save, sender=Refund)
def capture_old_refund_status(sender, instance, **kwargs):
    """
    Capture the old status before save to detect changes in post_save.
    """
    if instance.pk:
        try:
            old_instance = sender.objects.get(pk=instance.pk)
            instance._old_status = old_instance.status
        except sender.DoesNotExist:
            instance._old_status = None
    else:
        instance._old_status = None


@receiver(post_save, sender=Refund)
def handle_refund_status_change(sender, instance, created, **kwargs):
    """
    Trigger refund notification email when status changes to 'processed'.
    Only fires if status actually changed.
    """
    if created:
        return  # No email on creation (status is 'pending')

    old_status = getattr(instance, '_old_status', None)
    if old_status == instance.status:
        return  # No change

    # PROCESSED transition
    if instance.status == 'processed' and old_status != 'processed':
        if instance.transaction and instance.transaction.booking:
            booking = instance.transaction.booking
            recipient_email = (
                booking.customer.email if booking.customer
                else booking.guest_email
            )
            if recipient_email:
                try:
                    # Use dynamic currency from transaction; fallback to 'KES' if None
                    currency = instance.transaction.currency or 'KES'
                    send_refund_notification_email.delay(
                        booking.id,
                        str(instance.amount),  # Pass as string for template
                        instance.reason,
                        recipient_email,
                        currency  # NEW: Pass currency
                    )
                    logger.info(
                        f"Queued refund email for booking {booking.id} (refund {instance.id})")
                except Exception as e:
                    logger.error(
                        f"Failed to queue refund email for refund {instance.id}: {str(e)}"
                    )

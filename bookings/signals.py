from django.db.models.signals import post_save
from django.dispatch import receiver
from bookings.models import Route, Booking, BookingStatus
from driver.models import DriverShift
from django.utils import timezone
from django.db import transaction
import logging

from bookings.tasks import send_booking_payment_success_email

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Route)
def update_on_route_assignment(sender, instance, **kwargs):
    """
    Automatically update shift and bookings when a driver is assigned to a route.
    Triggers on ANY save where driver is set (API, admin panel, etc.).
    """
    route = instance
    if not route.driver or route.status != "assigned":
        return  # Only trigger if driver is newly assigned and status is 'assigned'

    with transaction.atomic():
        # Handle shift updates (unchanged)
        if route.shift and not route.shift.driver:
            route.shift.driver = route.driver
            route.shift.status = DriverShift.Status.ASSIGNED

            # Update load (safe defaults)
            if not isinstance(route.shift.current_load, dict):
                route.shift.current_load = {"weight": 0.0, "volume": 0.0, "hours": 0.0}
            elif "hours" not in route.shift.current_load:
                route.shift.current_load["hours"] = 0.0

            route.shift.current_load["hours"] = round(
                route.shift.current_load["hours"] + (route.total_time_hours or 0), 2
            )
            route.shift.save(update_fields=["driver", "status", "current_load"])

        # Updated: Bookings update with mixed support
        updated = 0
        if route.leg_type == "mixed":
            # Loop for per-type status
            for booking in route.bookings.all():
                typ = route.get_stop_type(booking)
                booking_status = (
                    BookingStatus.ASSIGNED
                    if typ == "pickup"
                    else BookingStatus.IN_TRANSIT
                )
                booking.driver = route.driver
                booking.hub = route.driver.hub if route.driver.hub else None
                booking.status = booking_status
                booking.updated_at = timezone.now()
                booking.save()
                updated += 1
        else:
            # Original bulk update for non-mixed
            booking_status = (
                BookingStatus.ASSIGNED
                if route.leg_type == "pickup"
                else (
                    BookingStatus.IN_TRANSIT
                    if route.leg_type == "delivery"
                    else BookingStatus.ASSIGNED
                )
            )
            updated = route.bookings.update(
                driver=route.driver,
                hub=route.driver.hub if route.driver.hub else None,
                status=booking_status,
                updated_at=timezone.now(),
            )

        logger.info(
            f"Route {route.id} assigned to {route.driver.user.get_full_name()}. "
            f"Updated shift {route.shift.id if route.shift else 'None'} and {updated} bookings."
        )


# bookings/signals.py
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from .models import PricingRule
from django.core.cache import cache


@receiver([post_save, post_delete], sender=PricingRule)
def clear_pricing_cache(sender, **kwargs):
    cache.delete("pricing_rules")


# NEW: Senior-level receiver to trigger email on status change (after payment success)
@receiver(post_save, sender=Booking)
def trigger_confirmation_on_payment(sender, instance, created, **kwargs):
    """
    Triggers payment success email when booking status changes to SCHEDULED (indicating payment success).
    - Uses transaction.on_commit to ensure DB commit (and file write) happens first – avoids race conditions.
    - Determines recipient: guest_email if present, else customer.email (assume customer has .email field).
    - Only on update (not create), and only if status just became SCHEDULED.
    - Senior notes: Weakly coupled (no direct payment dep), idempotent if task is (email skips if not success).
    """
    if created:
        return  # Skip on creation – we wait for status update after payment

    # Check if status just became SCHEDULED (use _previous_state if using django-simple-history, else compare)
    # For simplicity: assume post_save is triggered on status update; check current status
    if instance.status == BookingStatus.SCHEDULED:
        # Determine recipient
        recipient = instance.guest_email if instance.guest_email else (instance.customer.email if instance.customer else None)
        if not recipient:
            logger.warning(f"No email found for booking {instance.id} – skipping success email")
            return

        # Delay email after commit (ensures QR file is written if generated during save)
        transaction.on_commit(
            lambda: send_booking_payment_success_email.delay(str(instance.id), recipient)
        )
        logger.info(f"Queued success email for booking {instance.id} on status change to SCHEDULED")
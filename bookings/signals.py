from django.db.models.signals import post_save
from django.dispatch import receiver
from bookings.models import Route, Booking, BookingStatus
from driver.models import DriverShift
from django.utils import timezone
from django.db import transaction
import logging

logger = logging.getLogger(__name__)

@receiver(post_save, sender=Route)
def update_on_route_assignment(sender, instance, **kwargs):
    """
    Automatically update shift and bookings when a driver is assigned to a route.
    Triggers on ANY save where driver is set (API, admin panel, etc.).
    """
    route = instance
    if not route.driver or route.status != 'assigned':
        return  # Only trigger if driver is newly assigned and status is 'assigned'

    with transaction.atomic():
        # Handle shift updates
        if route.shift and not route.shift.driver:
            route.shift.driver = route.driver
            route.shift.status = DriverShift.Status.ASSIGNED

            # Update load (safe defaults)
            if not isinstance(route.shift.current_load, dict):
                route.shift.current_load = {"weight": 0.0, "volume": 0.0, "hours": 0.0}
            elif 'hours' not in route.shift.current_load:
                route.shift.current_load['hours'] = 0.0

            route.shift.current_load['hours'] = round(
                route.shift.current_load['hours'] + (route.total_time_hours or 0), 2
            )
            route.shift.save(update_fields=['driver', 'status', 'current_load'])

        # Update bookings
        updated = route.bookings.update(
            driver=route.driver,
            status=BookingStatus.ASSIGNED,
            hub=route.driver.hub if route.driver.hub else None,
            updated_at=timezone.now()
        )

        logger.info(
            f"Route {route.id} assigned to {route.driver.user.get_full_name()}. "
            f"Updated shift {route.shift.id if route.shift else 'None'} and {updated} bookings."
        )

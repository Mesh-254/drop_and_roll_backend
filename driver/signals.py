from django.db.models.signals import post_save
from django.dispatch import receiver
from django.db import transaction
from bookings.models import Booking, BookingStatus, Route
from driver.models import DriverShift, DriverProfile, DriverAvailability, DriverLocation
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from .serializers import DriverLocationSerializer


@receiver(post_save, sender=DriverProfile)
def create_and_set_availability(sender, instance, created, **kwargs):
    if created:
        # Create with available=True (new driver has no routes)
        availability, _ = DriverAvailability.objects.get_or_create(
            driver_profile=instance, defaults={"available": True}
        )
        print(
            f"Created availability for new driver {instance.user.email}: available={availability.available}"
        )  # For debug; remove later
    else:
        # On update (e.g., status change to active), recompute if needed
        if hasattr(instance, "availability"):
            instance.recompute_availability()


# sync latest location from DriverLocation to DriverAvailability
@receiver(post_save, sender=DriverLocation)
def update_driver_availability_location(sender, instance, created, **kwargs):
    if not created:
        return  # Only on new updates

    availability, _ = DriverAvailability.objects.get_or_create(
        driver_profile=instance.driver_profile
    )
    availability.lat = instance.latitude
    availability.lng = instance.longitude
    availability.last_updated = instance.timestamp
    # Optionally set available=True if driver is sending updates
    # Or keep logic in recompute_availability()
    availability.save(update_fields=["lat", "lng", "last_updated"])

    # Trigger any other logic (e.g., route progress detection)
    instance.driver_profile.recompute_availability()

    if created and instance.driver_profile.is_tracking_enabled:
        # Find active route for driver
        active_route = Route.objects.filter(
            driver=instance.driver_profile, status__in=["assigned", "in_progress"]
        ).first()
        if active_route:
            instance.route = active_route
            instance.save(update_fields=["route"])

    if instance.route:
        instance.route = (
            Route.objects.select_related("hub")
            .prefetch_related("bookings__pickup_address", "bookings__dropoff_address")
            .get(id=instance.route.id)
        )


@receiver(post_save, sender=Booking)
def update_shift_status_on_booking_change(sender, instance, **kwargs):
    for route in instance.route_set.all():
        if route.shift:
            route.shift.update_status()  # Recompute based on all bookings


@receiver(post_save, sender=Route)
def update_driver_availability_on_route_change(sender, instance, **kwargs):
    if not instance.driver:
        return

    driver = instance.driver
    availability = driver.availability

    # Check if driver has any incomplete routes (across all shifts)
    has_active_routes = Route.objects.filter(
        driver=driver, status__in=["assigned", "in_progress"]
    ).exists()

    # If no active routes → driver is available
    if not has_active_routes:
        if not availability.available:
            availability.available = True
            availability.save(update_fields=["available"])
    else:
        if availability.available:
            availability.available = False
            availability.save(update_fields=["available"])


# Also trigger on shift status change (e.g., when shift marked COMPLETED)
@receiver(post_save, sender=DriverShift)
def update_availability_on_shift_change(sender, instance, **kwargs):
    # Skip entirely if no driver is assigned yet
    if not instance.driver:
        return

    if instance.status in [DriverShift.Status.COMPLETED, DriverShift.Status.OVERDUE]:
        # Check if driver has any other incomplete routes
        has_active_routes = Route.objects.filter(
            driver=instance.driver, status__in=["assigned", "in_progress"]
        ).exists()
        availability = instance.driver.availability
        if not has_active_routes and not availability.available:
            availability.available = True
            availability.save(update_fields=["available"])


# In driver/signals.py, update the broadcast_location_update function to convert UUID to string:


@receiver(post_save, sender=DriverLocation)
def broadcast_location_update(sender, instance, created, **kwargs):
    if created:
        channel_layer = get_channel_layer()
        message = {
            "type": "location.update",
            "data": {
                "driver_id": str(instance.driver_profile.id),  # Convert UUID to str
                "latitude": float(instance.latitude),  # Ensure Decimal to float
                "longitude": float(instance.longitude),
                "speed_kmh": float(instance.speed_kmh) if instance.speed_kmh else None,
                "heading_degrees": instance.heading_degrees,
                "accuracy_meters": (
                    float(instance.accuracy_meters)
                    if instance.accuracy_meters
                    else None
                ),
                "timestamp": instance.timestamp.isoformat(),  # Convert datetime to ISO string
            },
        }
        if instance.route:
            message["data"]["route_id"] = str(
                instance.route.id
            )  # If route exists, convert UUID
        async_to_sync(channel_layer.group_send)("tracking", message)


@receiver(post_save, sender=Route)
def handle_route_assignment(sender, instance, **kwargs):
    if instance.driver and instance.status == "assigned":
        driver = instance.driver
        driver.recompute_availability()
        if not driver.is_tracking_enabled:
            driver.is_tracking_enabled = True
            driver.save(update_fields=["is_tracking_enabled"])


@receiver(post_save, sender=Route)
def handle_manual_or_cascaded_route_completion(sender, instance, **kwargs):
    """
    Handles manual/admin completion or cancellation of route.
    Slim version — only affects driver tracking/availability.
    """
    if instance.status not in ["completed", "cancelled"]:
        return

    if not instance.driver:
        return

    with transaction.atomic():
        driver = instance.driver
        driver.recompute_availability()

        has_active = Route.objects.filter(
            driver=driver, status__in=["assigned", "in_progress"]
        ).exists()

        if not has_active and driver.is_tracking_enabled:
            driver.is_tracking_enabled = False
            driver.save(update_fields=["is_tracking_enabled"])


@receiver(post_save, sender=Booking)
def handle_booking_assignment(sender, instance, **kwargs):
    if instance.driver and instance.status in [
        BookingStatus.ASSIGNED,
        BookingStatus.IN_TRANSIT,
    ]:
        driver = instance.driver
        driver.recompute_availability()  # Sets available=False if active
        if not driver.is_tracking_enabled:
            driver.is_tracking_enabled = True
            driver.save(update_fields=["is_tracking_enabled"])
            # Broadcast update via WS (optional for real-time driver app detect)
            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                f"driver_{driver.id}",  # Per-driver group
                {"type": "tracking.toggle", "enabled": True},
            )


@receiver(post_save, sender=Booking)
def handle_booking_completion(sender, instance, **kwargs):
    if instance.driver and instance.status in [
        BookingStatus.DELIVERED,
        BookingStatus.CANCELLED,
    ]:
        driver = instance.driver
        driver.recompute_availability()  # Sets available=True if no active bookings/routes
        has_active = (
            Route.objects.filter(
                driver=driver,
                status__in=[BookingStatus.ASSIGNED, BookingStatus.PICKED_UP],
            ).exists()
            or Booking.objects.filter(
                driver=driver,
                status__in=[BookingStatus.ASSIGNED, BookingStatus.PICKED_UP],
            ).exists()
        )
        if not has_active and driver.is_tracking_enabled:
            driver.is_tracking_enabled = False
            driver.save(update_fields=["is_tracking_enabled"])
            # Broadcast disable
            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                f"driver_{driver.id}", {"type": "tracking.toggle", "enabled": False}
            )

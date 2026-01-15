from django.db.models.signals import post_save
from django.dispatch import receiver
from bookings.models import Booking, Route
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


@receiver(post_save, sender=DriverLocation)
def broadcast_location_update(sender, instance, created, **kwargs):
    if not created:
        return  # Only broadcast new locations

    channel_layer = get_channel_layer()
    if channel_layer is None:
        return  # No channel layer (e.g., in tests or non-ASGI env)

    serializer = DriverLocationSerializer(instance)
    async_to_sync(channel_layer.group_send)(
        "tracking",
        {
            "type": "driver.location.update",  # Will call driver_location_update in consumer
            "data": serializer.data
        }
    )
# bookings/utils/hub_assignment.py
import logging
from django.db.models import Q
from geopy.distance import great_circle
from bookings.models import Hub, Booking, BookingStatus

logger = logging.getLogger(__name__)


def assign_to_nearest_hub(
    bookings_qs,
    force_reassign=False,
    max_distance_km=50,
    only_if_unassigned=True,  # new: control whether to skip already assigned
):
    """
    Assigns bookings to the nearest hub based **only** on pickup_address.

    - Assigns only if hub is None (unless force_reassign=True)
    - Uses pickup_address consistently (even for AT_HUB bookings)
    - If only_if_unassigned=True (default), skips bookings that already have a hub
      unless force_reassign=True
    """
    if not bookings_qs.exists():
        logger.info("No bookings to assign hub.")
        return 0

    # Only load hubs with valid coords
    hubs = list(
        Hub.objects.select_related("address").filter(
            address__latitude__isnull=False,
            address__longitude__isnull=False,
        )
    )

    if not hubs:
        logger.error("No hubs with coordinates found — cannot assign.")
        return 0

    updated_count = 0

    for booking in bookings_qs.iterator():  # iterator → lower memory for large qs
        # Use pickup_address ALWAYS for hub assignment decision
        addr = booking.pickup_address
        if not addr or addr.latitude is None or addr.longitude is None:
            logger.warning(f"Booking {booking.id} missing pickup coords — skipping.")
            continue

        # Skip if already assigned (unless forcing)
        if booking.hub and (not force_reassign or only_if_unassigned):
            continue

        # Find nearest hub
        nearest_hub = None
        min_dist = float("inf")
        pickup_point = (addr.latitude, addr.longitude)

        for hub in hubs:
            hub_point = (hub.address.latitude, hub.address.longitude)
            dist = great_circle(pickup_point, hub_point).km
            if dist < min_dist:
                min_dist = dist
                nearest_hub = hub

        if nearest_hub is None:
            logger.warning(f"No hub candidates for Booking {booking.id}")
            continue

        if max_distance_km and min_dist > max_distance_km:
            logger.warning(
                f"Booking {booking.id} nearest hub ({nearest_hub.name}) is "
                f"{min_dist:.1f} km > max {max_distance_km} km — leaving unassigned."
            )
            continue

        booking.hub = nearest_hub
        booking.save(update_fields=["hub"])
        updated_count += 1

        logger.info(
            f"Assigned Booking {booking.id} (status={booking.status}) to hub "
            f"{nearest_hub.name} ({min_dist:.1f} km from pickup)"
        )

    logger.info(f"Hub assignment completed: {updated_count} bookings updated.")
    return updated_count

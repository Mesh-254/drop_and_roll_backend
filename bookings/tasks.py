import os
from celery import shared_task
from django.core.mail import send_mail
from django.core.mail import EmailMultiAlternatives
from django.conf import settings
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from .models import Booking
from payments.models import PaymentTransaction
from bookings.utils.route_optimization import cluster_bookings, optimize_routes
from bookings.utils.distance_utils import get_time_matrix, distance
from datetime import timedelta
from django.utils import timezone
from bookings.models import BookingStatus, Route, Hub
from driver.models import DriverProfile, DriverAvailability, DriverShift
import logging
from collections import defaultdict
from django.db.models import (
    Exists,
    OuterRef,
    Prefetch,
    Case,
    When,
    F,
    FloatField,
    Value,
    CharField,
    IntegerField,
    Q,
)
from django.db.models.functions import Cast
from django.db import transaction
from dateutil.parser import parse
from geopy.distance import great_circle
from django.conf import settings

from bookings.utils.hub_assignment import assign_to_nearest_hub


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
        booking = Booking.objects.select_related("quote", "customer").get(id=booking_id)
        payment = PaymentTransaction.objects.get(
            booking=booking
        )  # Assume 1:1; adjust if needed

        if payment.status != "success":  # PaymentStatus.SUCCESS
            return  # Bail if not success

        # Generate QR (skips if exists)
        # Generate QR if not exists (your generate_qr() should be idempotent)
        if not booking.qr_code_url:
            try:
                qr_url = booking.generate_qr(
                    force_regenerate=False
                )  # Avoid force unless needed
                if qr_url:
                    booking.qr_code_url = qr_url
                    booking.save(update_fields=["qr_code_url"])
                    logger.info(
                        f"Generated QR for booking {booking_id} during success email"
                    )
                else:
                    logger.warning(
                        f"generate_qr() returned empty for booking {booking_id}"
                    )
            except Exception as e:
                logger.exception(f"QR generation failed for booking {booking_id}: {e}")

        subject = f" Payment Successful: Booking #{booking.id} Confirmed ! "

        context = {
            "booking": booking,
            "payment": payment,
            "qr_url": booking.qr_code_url or "",
            "site_name": "Drop 'n Roll",
            "support_email": settings.DEFAULT_FROM_EMAIL,
            "tracking_url": f"{settings.FRONTEND_URL}/track/{booking.tracking_number or booking.id}",
        }
        html_message = render_to_string("emails/booking_payment_success.html", context)
        plain_message = strip_tags(html_message)

        email = EmailMultiAlternatives(
            subject=subject,
            body=plain_message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient_email],
        )
        email.attach_alternative(html_message, "text/html")  # Attach HTML version

        # Attach QR image from disk
        if booking.qr_code_url:
            # Convert URL path (e.g., "/media/qr/xxx.png") to filesystem path
            # Strip MEDIA_URL prefix (e.g., "/media/") and leading slashes
            relative_path = booking.qr_code_url.replace(
                settings.MEDIA_URL, "", 1
            ).lstrip("/")
            absolute_path = os.path.join(settings.MEDIA_ROOT, relative_path)

            if os.path.isfile(absolute_path):  # Use isfile() for safety
                with open(absolute_path, "rb") as f:
                    email.attach(
                        filename=f"QR_{booking.tracking_number or str(booking.id)[:8]}.png",
                        content=f.read(),
                        mimetype="image/png",
                    )
                logger.info(
                    f"Attached QR for booking {booking_id} from {absolute_path}"
                )
            else:
                logger.error(
                    f"QR file not found at {absolute_path} for booking {booking_id} – sending without attachment"
                )

        # Send and log
        sent_count = email.send(fail_silently=False)
        if sent_count > 0:
            logger.info(
                f"Payment success email sent for booking {booking_id} to {recipient_email}"
            )
        else:
            logger.warning(
                f"Email sending failed (returned {sent_count}) for booking {booking_id}"
            )

    except (Booking.DoesNotExist, PaymentTransaction.DoesNotExist):
        # Log if needed: logger.error(f"Booking/Payment {booking_id} not found")
        pass


@shared_task
def send_booking_payment_failure_email(
    booking_id, recipient_email, failure_reason="Payment did not succeed"
):
    """Send combined failure email: Booking details + payment failed."""
    try:
        booking = Booking.objects.select_related("quote", "customer").get(id=booking_id)
        payment = PaymentTransaction.objects.get(booking=booking)
        if payment.status != "failed":  # PaymentStatus.FAILED
            return  # Bail if not failure

        subject = f"Booking #{booking.id} – Payment Failed: Action Required"
        context = {
            "booking": booking,
            "payment": payment,
            "failure_reason": failure_reason,
            "site_name": "Drop 'n Roll",
            "support_email": settings.DEFAULT_FROM_EMAIL,
            # Adjust to your frontend route
            "new_booking_url": f"{settings.FRONTEND_URL}/booking",
        }
        html_message = render_to_string("emails/booking_payment_failure.html", context)
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


# *****************************************************************
# task to optimize bookings into routes and assign drivers
# ******************************************************************


logger = logging.getLogger(__name__)
# ----------------------------------------------------------------------
# EXPANDED SERVICE-TYPE MAPPING (configurable – add more as needed)
# ----------------------------------------------------------------------
SERVICE_TYPE_TO_BUCKET = {
    # Urgent/same-day tiers (priority 0)
    "Same Day": "same_day",
    "Express": "same_day",
    "Same-Day": "same_day",
    "Golden Hour": "same_day",
    "Urgent": "same_day",
    "Golden": "same_day",
    # Next-day tiers (priority 1)
    "Next Day": "next_day",
    "Standard": "next_day",
    # 3-day/economy tiers (priority 2)
    "Economy": "three_day",
    "Three Day": "three_day",
    "Budget": "three_day",
}

MIN_ROUTE_HOURS = 2
MAX_DAILY_HOURS = 10.0
HUB_PROXIMITY_KM = 50.0


def get_bucket_name(booking):
    """Helper to get bucket from service type name"""
    name = booking.quote.service_type.name
    return SERVICE_TYPE_TO_BUCKET.get(name, "three_day")


def get_stop_address(booking, stop_type):
    """Helper to get correct address depending on stop type"""
    if stop_type == "pickup":
        return booking.pickup_address
    else:
        return booking.dropoff_address


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def optimize_bookings(self):
    now = timezone.now()
    logger.info("Starting route optimization task")

    MIXED_ROUTES = getattr(
        settings, "MIXED_ROUTES", False
    )  # Config flag: Set in settings.py; default False for safety

    # ==================================================================
    # STEP 0: Proximity-based hub assignment (NEW — runs once per task)
    # ==================================================================

    # Get all candidates that might need assignment
    candidates = Booking.objects.filter(
        status__in=[BookingStatus.SCHEDULED,],
        pickup_address__latitude__isnull=False,
        dropoff_address__latitude__isnull=False,
    )

    # Assign only those without hub (or force=True if you want to re-evaluate)
    assigned_count = assign_to_nearest_hub(
        candidates.filter(hub__isnull=True),   # only unassigned
        force_reassign=False,                  # Set True in dev to re-assign all
        max_distance_km=50                     # Optional safety
    )
    logger.info(f"Proximity assignment: {assigned_count} bookings got a hub.")

    # ------------------------------------------------------------------
    # 1. Separate pickup (SCHEDULED) and delivery (AT_HUB) candidates
    # ------------------------------------------------------------------

    # Common Case expression for bucket
    service_case = Case(
        *[
            When(quote__service_type__name__iexact=k, then=Value(v))
            for k, v in SERVICE_TYPE_TO_BUCKET.items()
        ],
        default=Value("three_day"),
        output_field=CharField(),
    )

    # ──────────────────────────────────────────────────────────────
    # 1. Fetch candidates (same query structure as before)
    # ──────────────────────────────────────────────────────────────

    pickup_candidates = (
        Booking.objects.filter(
            ~Exists(Route.objects.filter(bookings=OuterRef("pk"))),
            status=BookingStatus.SCHEDULED,
            pickup_address__latitude__isnull=False,
            dropoff_address__latitude__isnull=False,
        )
        .annotate(
            bucket=service_case,
        )
        .prefetch_related("quote__service_type", "pickup_address", "dropoff_address")
    )


    delivery_candidates = (
        Booking.objects.filter(
            ~Exists(
                Route.objects.filter(bookings=OuterRef("pk"), leg_type="delivery")
            ),  # FIXED: Not in a DELIVERY route (can be in pickup)
            status=BookingStatus.AT_HUB,
            pickup_address__latitude__isnull=False,
            dropoff_address__latitude__isnull=False,
        )
        .annotate(
            bucket=service_case,
        )
        .prefetch_related("quote__service_type", "pickup_address", "dropoff_address")
    )

    # ──────────────────────────────────────────────────────────────
    # 2. Process each hub
    # ──────────────────────────────────────────────────────────────

    # Group by hub and bucket (reconstructed from truncated code)
    for hub in Hub.objects.all():
        hub_lat = hub.address.latitude
        hub_lng = hub.address.longitude

        # ──────────────────────────────────────────────────────────────
        # A. Separate path (current behavior when MIXED_ROUTES=False)
        # ──────────────────────────────────────────────────────────────
        if not MIXED_ROUTES:
            # Filter candidates near hub (using geopy for distance; adjust radius as needed)

            # Pickups & deliveries near hub
            hub_pickups = [b for b in pickup_candidates if b.hub_id == hub.id]
            hub_deliveries = [b for b in delivery_candidates if b.hub_id == hub.id]


            # Bucket them
            bucketed_pickups = defaultdict(list)
            for b in hub_pickups:
                bucketed_pickups[get_bucket_name(b)].append(b)

            bucketed_deliveries = defaultdict(list)
            for b in hub_deliveries:
                bucketed_deliveries[get_bucket_name(b)].append(b)

            # ──────────────────────────────────────────────────────────────
            # Process each bucket – same_day first (priority)
            # ──────────────────────────────────────────────────────────────

            # Process buckets in priority order
            for bucket_priority in ["same_day", "next_day", "three_day"]:
                # Pickups
                pickups = bucketed_pickups[bucket_priority]
                if pickups:
                    # Get matrices (time/distance)
                    time_matrix, distance_matrix = get_time_matrix(
                        [b.pickup_address for b in pickups], hub_lat, hub_lng
                    )
                    # Get drivers for hub (assuming availability check)
                    drivers = (
                        DriverProfile.objects.filter(hub=hub)
                        .filter(availability__available=True)
                        .select_related("user", "availability")
                    )
                    # Optimize - FIXED: Use keyword args to avoid positional errors
                    routes = optimize_routes(
                        bookings=pickups,
                        drivers=drivers,
                        hub_lat=hub_lat,
                        hub_lng=hub_lng,
                        leg_type="pickup",
                    )  # time_windows=None, stop_types=None by default

                    # Assign routes
                    for ordered, hrs, km, driver, etas in routes:
                        if hrs < MIN_ROUTE_HOURS:
                            logger.info(
                                f"Skipping small route: {hrs:.2f}h < {MIN_ROUTE_HOURS}h"
                            )
                            continue

                        ordered_stops = [
                            {
                                "booking_id": str(b.id),
                                "address": {
                                    "lat": float(b.pickup_address.latitude),
                                    "lng": float(b.pickup_address.longitude),
                                },
                                "eta": eta.isoformat() if eta else None,
                            }
                            for b, eta in zip(ordered, etas)
                        ]

                        _create_or_assign_route(
                            ordered=ordered,
                            hrs=hrs,
                            km=km,
                            driver=driver,
                            etas=etas,
                            ordered_stops=ordered_stops,
                            hub=hub,
                            leg_type="pickup",
                            bucket=bucket_priority,
                            now=now,
                        )

                # Deliveries
                deliveries = bucketed_deliveries[bucket_priority]
                if deliveries:
                    time_matrix, distance_matrix = get_time_matrix(
                        [b.dropoff_address for b in deliveries], hub_lat, hub_lng
                    )
                    drivers = (
                        DriverProfile.objects.filter(hub=hub)
                        .filter(availability__available=True)
                        .select_related("user", "availability")
                    )
                    # Optimize - FIXED: Use keyword args
                    routes = optimize_routes(
                        bookings=deliveries,
                        drivers=drivers,
                        hub_lat=hub_lat,
                        hub_lng=hub_lng,
                        leg_type="delivery",
                    )

                    for ordered, hrs, km, driver, etas in routes:
                        if hrs < MIN_ROUTE_HOURS:
                            logger.info(
                                f"Skipping small route: {hrs:.2f}h < {MIN_ROUTE_HOURS}h"
                            )
                            continue

                        ordered_stops = [
                            {
                                "booking_id": str(b.id),
                                "address": {
                                    "lat": float(b.dropoff_address.latitude),
                                    "lng": float(b.dropoff_address.longitude),
                                },
                                "eta": eta.isoformat() if eta else None,
                            }
                            for b, eta in zip(ordered, etas)
                        ]

                        _create_or_assign_route(
                            ordered=ordered,
                            hrs=hrs,
                            km=km,
                            driver=driver,
                            etas=etas,
                            ordered_stops=ordered_stops,
                            hub=hub,
                            leg_type="delivery",
                            bucket=bucket_priority,
                            now=now,
                        )

        # ──────────────────────────────────────────────────────────────
        # B. Mixed route path (when MIXED_ROUTES=True)
        # ──────────────────────────────────────────────────────────────

        else:
            # Collect all candidates near this hub
            hub_candidates = []
            for b in pickup_candidates:
                if b.hub_id == hub.id:
                    hub_candidates.append((b, "pickup"))
            for b in delivery_candidates:
                if b.hub_id == hub.id:
                    hub_candidates.append((b, "delivery"))

            if not hub_candidates:
                continue

            # Bucket by service level (no cross-bucket mixing)
            bucketed_mixed = defaultdict(list)
            for booking, stop_type in hub_candidates:
                bucket = get_bucket_name(booking)
                bucketed_mixed[bucket].append((booking, stop_type))

            # Drivers available at this hub
            drivers = (
                DriverProfile.objects.filter(hub=hub)
                .filter(availability__available=True)
                .select_related("user", "availability")
            )

            # Process each bucket (priority order)
            for bucket in ["same_day", "next_day", "three_day"]:
                mixed_items = bucketed_mixed[bucket]
                if len(mixed_items) < 2:
                    continue  # too few to justify mixed route

                bookings = [item[0] for item in mixed_items]
                stop_types = [item[1] for item in mixed_items]

                # Use correct address per stop type
                addresses = [get_stop_address(b, t) for b, t in mixed_items]

                time_matrix, distance_matrix = get_time_matrix(
                    addresses, hub_lat, hub_lng
                )

                # Optimize - FIXED: Use keyword args
                routes = optimize_routes(
                    bookings=bookings,
                    drivers=drivers,
                    hub_lat=hub_lat,
                    hub_lng=hub_lng,
                    stop_types=stop_types,
                    leg_type="mixed",
                )

                for ordered, hrs, km, driver, etas in routes:
                    if hrs < MIN_ROUTE_HOURS:
                        logger.info(
                            f"Skipping small mixed route ({bucket}): {hrs:.2f}h"
                        )
                        continue

                    # Build ordered_stops with type information
                    ordered_stops = []
                    for i, b in enumerate(ordered):
                        # Find corresponding stop type
                        idx = bookings.index(b)
                        typ = stop_types[idx]
                        addr = get_stop_address(b, typ)

                        ordered_stops.append(
                            {
                                "booking_id": str(b.id),
                                "type": typ,
                                "address": {
                                    "lat": float(addr.latitude),
                                    "lng": float(addr.longitude),
                                },
                                "eta": (
                                    etas[i].isoformat()
                                    if etas and i < len(etas)
                                    else None
                                ),
                            }
                        )

                    _create_or_assign_route(
                        ordered=ordered,
                        hrs=hrs,
                        km=km,
                        driver=driver,
                        etas=etas,
                        ordered_stops=ordered_stops,
                        hub=hub,
                        leg_type="mixed",
                        bucket=bucket,
                        now=now,
                        mixed_stop_types=stop_types,  # optional, for logging
                    )

    logger.info("Route optimization completed successfully")


def _create_or_assign_route(
    ordered,
    hrs,
    km,
    driver,
    etas,
    ordered_stops,
    hub,
    leg_type,
    bucket,
    now,
    mixed_stop_types=None,
):
    """
    Shared logic to create pending or assigned route + update bookings/shift
    """
    with transaction.atomic():
        if not driver:
            pending_shift = DriverShift.objects.create(
                driver=None,
                start_time=now.replace(hour=8, minute=0, second=0, microsecond=0),
                end_time=now.replace(hour=18, minute=0, second=0, microsecond=0),
                status=DriverShift.Status.PENDING,
                current_load={"weight": 0.0, "volume": 0.0, "hours": 0.0},
            )

            route = Route.objects.create(
                driver=None,
                shift=pending_shift,
                leg_type=leg_type,
                ordered_stops=ordered_stops,
                total_time_hours=round(hrs, 3),
                total_distance_km=round(km, 3),
                status="pending",
                visible_at=now,
                hub=hub,
            )
            route.bookings.set(ordered)
            route.save()  # trigger validation

            # Reset bookings to original status
            status_map = {
                "pickup": BookingStatus.SCHEDULED,
                "delivery": BookingStatus.AT_HUB,
                "mixed": BookingStatus.SCHEDULED,  # conservative
            }
            default_status = status_map.get(leg_type, BookingStatus.SCHEDULED)

            Booking.objects.filter(id__in=[b.id for b in ordered]).update(
                hub=hub,
                driver=None,
                status=default_status,
                updated_at=now,
            )

            logger.info(
                f"PENDING {leg_type.upper()} ROUTE CREATED | Hub: {hub.name} | "
                f"Bucket: {bucket.upper()} | {len(ordered)} stops | {hrs:.2f}h | {km:.1f}km"
            )
            return

        # ─── Driver found ─────────────────────────────────────────────
        shift = DriverShift.get_or_create_today(driver)

        current_hours = (shift.current_load or {}).get("hours", 0.0)
        projected_hours = current_hours + hrs

        if projected_hours > MAX_DAILY_HOURS:
            logger.info(
                f"Skipping – exceeds {MAX_DAILY_HOURS}h: {projected_hours:.1f}h"
            )
            return

        if hrs < MIN_ROUTE_HOURS:
            logger.info(f"Skipping small route: {hrs:.2f}h < {MIN_ROUTE_HOURS}h")
            return

        shift.status = DriverShift.Status.ASSIGNED
        shift.current_load["hours"] = round(projected_hours, 2)
        shift.save(update_fields=["status", "current_load"])

        route = Route.objects.create(
            driver=driver,
            shift=shift,
            leg_type=leg_type,
            ordered_stops=ordered_stops,
            total_time_hours=round(hrs, 3),
            total_distance_km=round(km, 3),
            status="assigned",
            visible_at=now,
            hub=hub,
        )
        route.bookings.set(ordered)
        route.save()  # trigger validation

        # Set correct status per stop type (very important for mixed)
        for b in ordered:
            if leg_type != "mixed":
                status = (
                    BookingStatus.ASSIGNED
                    if leg_type == "pickup"
                    else BookingStatus.IN_TRANSIT
                )
            else:
                # Find type from ordered_stops or mixed_stop_types
                stop = next(
                    (s for s in ordered_stops if s["booking_id"] == str(b.id)), None
                )
                typ = stop["type"] if stop else "delivery"  # fallback
                status = (
                    BookingStatus.ASSIGNED
                    if typ == "pickup"
                    else BookingStatus.IN_TRANSIT
                )

            b.driver = driver
            b.hub = hub
            b.status = status
            b.updated_at = now
            b.save()

        logger.info(
            f"ROUTE ASSIGNED | Hub: {hub.name} | Driver: {driver.user.get_full_name()} "
            f"| {leg_type.upper()} | {bucket.upper()} | {len(ordered)} stops | "
            f"{hrs:.2f}h (+{current_hours:.1f}h → {projected_hours:.1f}h) | {km:.1f}km "
            f"| Shift: {shift.id} → {shift.status}"
        )


@shared_task
def send_route_email(route_id):
    route = Route.objects.get(id=route_id)
    driver_email = route.driver.user.email
    subject = f"Your Shift for {route.shift.start_time.date()}: Route Details"
    message = f"Route ID: {route.id}\nLeg: {route.leg_type}\nStops: {len(route.ordered_stops)}\nHours: {route.total_time_hours}\nDistance: {route.total_distance_km} km\nStatus: {route.status}"
    send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [driver_email])


# Run a daily/ hourly beat task to mark overdue shifts
@shared_task
def mark_overdue_shifts():
    now = timezone.now()
    overdue_shifts = DriverShift.objects.filter(
        end_time__lt=now,
        status__in=[DriverShift.Status.ASSIGNED, DriverShift.Status.ACTIVE],
    )
    for shift in overdue_shifts:
        shift.status = DriverShift.Status.OVERDUE
        shift.save()
    logger.info(f"Marked {overdue_shifts.count()} shifts as overdue")

from celery import shared_task
from django.core.mail import send_mail
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
from django.db.models import Exists, OuterRef, Prefetch, Case, When, F, FloatField, Value, CharField, IntegerField, Q
from django.db.models.functions import Cast
from django.db import transaction
from dateutil.parser import parse
from geopy.distance import great_circle


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
        booking = Booking.objects.select_related(
            'quote', 'customer').get(id=booking_id)
        payment = PaymentTransaction.objects.get(
            booking=booking)  # Assume 1:1; adjust if needed
        if payment.status != 'success':  # PaymentStatus.SUCCESS
            return  # Bail if not success

        subject = f' Payment Successful: Booking #{booking.id} Confirmed ! '
        context = {
            'booking': booking,
            'payment': payment,
            'site_name': 'Drop \'n Roll',
            'support_email': settings.DEFAULT_FROM_EMAIL,
        }
        html_message = render_to_string(
            'emails/booking_payment_success.html', context)
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
        # Log if needed: logger.error(f"Booking/Payment {booking_id} not found")
        pass


@shared_task
def send_booking_payment_failure_email(booking_id, recipient_email, failure_reason='Payment did not succeed'):
    """Send combined failure email: Booking details + payment failed."""
    try:
        booking = Booking.objects.select_related(
            'quote', 'customer').get(id=booking_id)
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
            # Adjust to your frontend route
            'new_booking_url': f"{settings.FRONTEND_URL}/booking",
        }
        html_message = render_to_string(
            'emails/booking_payment_failure.html', context)
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
    'Same Day': 'same_day',
    'Express': 'same_day',
    'Same-Day': 'same_day',
    'Golden Hour': 'same_day',
    'Urgent': 'same_day',
    'Golden': 'same_day',

    # Next-day tiers (priority 1)
    'Next Day': 'next_day',
    'Standard': 'next_day',

    # 3-day/economy tiers (priority 2)
    'Economy': 'three_day',
    'Three Day': 'three_day',
    'Budget': 'three_day',
}

MIN_ROUTE_HOURS = 2

@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def optimize_bookings(self):
    now = timezone.now()
    logger.info("Starting route optimization task")

    # ------------------------------------------------------------------
    # 1. Separate pickup (SCHEDULED) and delivery (AT_HUB) candidates
    # ------------------------------------------------------------------
    service_case = Case(
        *[When(quote__service_type__name__iexact=k, then=Value(v))
          for k, v in SERVICE_TYPE_TO_BUCKET.items()],
        default=Value('three_day'),
        output_field=CharField()
    )

    pickup_candidates = Booking.objects.filter(
        ~Exists(Route.objects.filter(bookings=OuterRef('pk'))),
        status=BookingStatus.SCHEDULED,
        pickup_address__latitude__isnull=False,
        dropoff_address__latitude__isnull=False,
       
    ).annotate(
        bucket=service_case,
    ).prefetch_related(
        'quote__service_type',
        'pickup_address',
        'dropoff_address'
    )

    delivery_candidates = Booking.objects.filter(
        ~Exists(Route.objects.filter(bookings=OuterRef('pk'), leg_type='delivery')),  # FIXED: Not in a DELIVERY route (can be in pickup)
        status=BookingStatus.AT_HUB,
        pickup_address__latitude__isnull=False,
        dropoff_address__latitude__isnull=False,
    ).annotate(
        bucket=service_case,
    ).prefetch_related(
        'quote__service_type',
        'pickup_address',
        'dropoff_address'
    )

    # Group by hub and bucket (reconstructed from truncated code)
    for hub in Hub.objects.all():
        hub_lat = hub.address.latitude
        hub_lng = hub.address.longitude

        # Filter candidates near hub (using geopy for distance; adjust radius as needed)
        hub_pickups = [b for b in pickup_candidates if great_circle((b.pickup_address.latitude, b.pickup_address.longitude), (hub_lat, hub_lng)).km < 50 and (not b.hub or b.hub == hub)]  # Example radius
        hub_deliveries = [b for b in delivery_candidates if great_circle((b.dropoff_address.latitude, b.dropoff_address.longitude), (hub_lat, hub_lng)).km < 50 and (not b.hub or b.hub == hub)]

        # FIXED: Assign hub to bookings in this group (only if null, to preserve existing)
        pickup_ids_to_assign = [b.id for b in hub_pickups if not b.hub]
        if pickup_ids_to_assign:
            Booking.objects.filter(id__in=pickup_ids_to_assign).update(hub=hub)
            for b in hub_pickups:
                if b.id in pickup_ids_to_assign:
                    b.hub = hub
            logger.info(f"Assigned hub {hub.name} to {len(pickup_ids_to_assign)} pickup bookings based on proximity")

        delivery_ids_to_assign = [b.id for b in hub_deliveries if not b.hub]
        if delivery_ids_to_assign:
            Booking.objects.filter(id__in=delivery_ids_to_assign).update(hub=hub)
            for b in hub_deliveries:
                if b.id in delivery_ids_to_assign:
                    b.hub = hub
            logger.info(f"Assigned hub {hub.name} to {len(delivery_ids_to_assign)} delivery bookings based on proximity")
        
        # Bucket them
        bucketed_pickups = defaultdict(list)
        for b in hub_pickups:
            bucketed_pickups[b.bucket].append(b)

        bucketed_deliveries = defaultdict(list)
        for b in hub_deliveries:
            bucketed_deliveries[b.bucket].append(b)

        # Process buckets in priority order
        for bucket in ['same_day', 'next_day', 'three_day']:
            # Pickups
            pickups = bucketed_pickups[bucket]
            if pickups:
                # Get matrices (time/distance)
                time_matrix, distance_matrix = get_time_matrix([b.pickup_address for b in pickups], hub_lat, hub_lng)
                # Get drivers for hub (assuming availability check)
                drivers = DriverProfile.objects.filter(hub=hub).filter(availability__available=True).select_related('user', 'availability')
                # Optimize
                routes = optimize_routes(pickups, hub_lat, hub_lng, time_matrix, distance_matrix, drivers, leg_type='pickup')

                # Line-by-line implementation starts here for pickups
                for ordered, hrs, km, driver, etas in routes:  # Existing: Loop over optimized route tuples (bookings list, hours, km, driver or None, ETAs)
                    if hrs < MIN_ROUTE_HOURS:
                        logger.info(f"Skipping small route: {hrs:.2f}h < {MIN_ROUTE_HOURS}h")
                        continue

                    ordered_stops = [  # Existing: Build JSON for stops (booking_id, address coords, eta)
                        {
                            'booking_id': str(b.id),
                            'address': {'lat': float(b.pickup_address.latitude), 'lng': float(b.pickup_address.longitude)},
                            'eta': eta.isoformat() if eta else None
                        } for b, eta in zip(ordered, etas)
                    ]

                    with transaction.atomic():  # Existing: Ensure atomicity for creation and updates
                        if not driver:  # Existing: CASE 1 - No driver (pending route)
                            pending_shift = DriverShift.objects.create(  # Existing: Create pending shift
                                driver=None,  # Explicitly null
                                start_time=now.replace(hour=8, minute=0, second=0, microsecond=0),  # Or your logic
                                end_time=now.replace(hour=18, minute=0, second=0, microsecond=0),
                                status=DriverShift.Status.PENDING,
                                current_load={'weight': 0.0, 'volume': 0.0, 'hours': 0.0},
                            )

                            route = Route.objects.create(  # Existing: Create route; NEW: Add hub=hub for explicit set
                                driver=None,
                                shift=pending_shift,
                                leg_type='pickup',  # Adjusted for leg_type
                                ordered_stops=ordered_stops,
                                total_time_hours=round(hrs, 3),
                                total_distance_km=round(km, 3),
                                status='pending',
                                visible_at=now,
                                hub=hub,  # NEW: Explicitly set hub from the loop context (ensures initial value even if no bookings)
                            )
                            route.bookings.set(ordered)  # Existing: Link bookings to route via M2M

                            # NEW: Force a save() call post-creation and post-set to trigger the hybrid inference/validation in Route.save()
                            # Why? create() saves initially without bookings; set() updates M2M; save() re-runs to check consistency.
                            # Safe in atomic: Rolls back if validation fails (e.g., mixed hubs).
                            route.save()

                            # Existing: Update bookings (keep SCHEDULED until assigned; set hub explicitly for consistency)
                            Booking.objects.filter(id__in=[b.id for b in ordered]).update(
                                hub=hub,
                                driver=None,
                                status=BookingStatus.SCHEDULED,
                                updated_at=now
                            )

                            logger.info(  # Existing: Log creation
                                f"PENDING ROUTE CREATED | Hub: {hub.name} | Driver: Unassigned "
                                f"| PICKUP | {bucket.upper()} | {len(ordered)} stops | {hrs:.2f}h | {km:.1f}km "
                                f"| Shift: {pending_shift.id}"
                            )
                            continue  # Existing: Skip to next route tuple

                        # Existing: CASE 2 - Driver found; apply rules and assign
                        shift = DriverShift.get_or_create_today(driver)  # Existing: Get/create shift

                        # Existing: 10-hour limit check
                        current_hours = (shift.current_load or {}).get('hours', 0.0)
                        projected_hours = current_hours + hrs

                        if projected_hours > 10.0:
                            logger.info(
                                f"Skipping route – {driver.user.get_full_name()} would exceed 10h limit "
                                f"({projected_hours:.1f}h > 10.0h)"
                            )
                            continue

                        # Existing: Min hours rule for non-same-day
                        if hrs < MIN_ROUTE_HOURS:
                            logger.info(
                                f"Skipping small route for {driver.user.get_full_name()} "
                                f"({hrs:.1f}h < {MIN_ROUTE_HOURS}h minimum)"
                            )
                            continue

                        # Existing: Update shift to assigned
                        shift.status = DriverShift.Status.ASSIGNED
                        shift.current_load['hours'] = round(projected_hours, 2)
                        shift.save(update_fields=['status', 'current_load'])

                        route = Route.objects.create(  # Existing: Create route; NEW: Add hub=hub
                            driver=driver,
                            shift=shift,
                            leg_type='pickup',
                            ordered_stops=ordered_stops,
                            total_time_hours=round(hrs, 3),
                            total_distance_km=round(km, 3),
                            status='assigned',
                            visible_at=now,
                            hub=hub,  # NEW: Explicit set
                        )
                        route.bookings.set(ordered)  # Existing: Link bookings

                        # NEW: Force save() to trigger validation (same as above)
                        route.save()

                        # Existing: Update bookings to assigned with driver/hub
                        Booking.objects.filter(id__in=[b.id for b in ordered]).update(
                            hub=hub,
                            driver=driver,
                            status=BookingStatus.ASSIGNED, # Explicit for pickup
                            updated_at=now
                        )

                        logger.info(  # Existing: Log assignment
                            f"ROUTE ASSIGNED | Hub: {hub.name} | Driver: {driver.user.get_full_name()} "
                            f"| PICKUP | {bucket.upper()} | {len(ordered)} stops | {hrs:.2f}h (+{current_hours:.1f}h → {projected_hours:.1f}h) | {km:.1f}km "
                            f"| Shift: {shift.id} → {shift.status}"
                        )

            # Repeat the same for deliveries (symmetric to pickups)
            deliveries = bucketed_deliveries[bucket]
            if deliveries:
                time_matrix, distance_matrix = get_time_matrix([b.dropoff_address for b in deliveries], hub_lat, hub_lng)
                drivers = DriverProfile.objects.filter(hub=hub).filter(availability__available=True).select_related('user', 'availability')
                routes = optimize_routes(deliveries, hub_lat, hub_lng, time_matrix, distance_matrix, drivers, leg_type='delivery')

                # Line-by-line for deliveries (mirror of pickups, with leg_type='delivery' and status updates adjusted)
                for ordered, hrs, km, driver, etas in routes:
                    if hrs < MIN_ROUTE_HOURS:
                        logger.info(f"Skipping small route: {hrs:.2f}h < {MIN_ROUTE_HOURS}h")
                        continue

                    ordered_stops = [
                        {
                            'booking_id': str(b.id),
                            'address': {'lat': float(b.dropoff_address.latitude), 'lng': float(b.dropoff_address.longitude)},
                            'eta': eta.isoformat() if eta else None
                        } for b, eta in zip(ordered, etas)
                    ]

                    with transaction.atomic():
                        if not driver:
                            pending_shift = DriverShift.objects.create(
                                driver=None,
                                start_time=now.replace(hour=8, minute=0, second=0, microsecond=0),
                                end_time=now.replace(hour=18, minute=0, second=0, microsecond=0),
                                status=DriverShift.Status.PENDING,
                                current_load={'weight': 0.0, 'volume': 0.0, 'hours': 0.0},
                            )

                            route = Route.objects.create(
                                driver=None,
                                shift=pending_shift,
                                leg_type='delivery',
                                ordered_stops=ordered_stops,
                                total_time_hours=round(hrs, 3),
                                total_distance_km=round(km, 3),
                                status='pending',
                                visible_at=now,
                                hub=hub,  # NEW: Explicit set
                            )
                            route.bookings.set(ordered)

                            # NEW: Force save() for validation
                            route.save()

                            # Existing: Update bookings (adjusted for delivery; keep AT_HUB until assigned)
                            Booking.objects.filter(id__in=[b.id for b in ordered]).update(
                                hub=hub,
                                driver=None,
                                status=BookingStatus.AT_HUB,
                                updated_at=now
                            )

                            logger.info(
                                f"PENDING ROUTE CREATED | Hub: {hub.name} | Driver: Unassigned "
                                f"| DELIVERY | {bucket.upper()} | {len(ordered)} stops | {hrs:.2f}h | {km:.1f}km "
                                f"| Shift: {pending_shift.id}"
                            )
                            continue

                        shift = DriverShift.get_or_create_today(driver)

                        current_hours = (shift.current_load or {}).get('hours', 0.0)
                        projected_hours = current_hours + hrs

                        if projected_hours > 10.0:
                            logger.info(
                                f"Skipping route – {driver.user.get_full_name()} would exceed 10h limit "
                                f"({projected_hours:.1f}h > 10.0h)"
                            )
                            continue

                        if hrs < MIN_ROUTE_HOURS:
                            logger.info(
                                f"Skipping small route for {driver.user.get_full_name()} "
                                f"({hrs:.1f}h < {MIN_ROUTE_HOURS}h minimum)"
                            )
                            continue

                        shift.status = DriverShift.Status.ASSIGNED
                        shift.current_load['hours'] = round(projected_hours, 2)
                        shift.save(update_fields=['status', 'current_load'])

                        route = Route.objects.create(
                            driver=driver,
                            shift=shift,
                            leg_type='delivery',
                            ordered_stops=ordered_stops,
                            total_time_hours=round(hrs, 3),
                            total_distance_km=round(km, 3),
                            status='assigned',
                            visible_at=now,
                            hub=hub,  # NEW: Explicit set
                        )
                        route.bookings.set(ordered)

                        # NEW: Force save() for validation
                        route.save()

                        # Existing: Update bookings (to IN_TRANSIT for delivery)
                        Booking.objects.filter(id__in=[b.id for b in ordered]).update(
                            hub=hub,
                            driver=driver,
                            status=BookingStatus.IN_TRANSIT,
                            updated_at=now
                        )

                        logger.info(
                            f"ROUTE ASSIGNED | Hub: {hub.name} | Driver: {driver.user.get_full_name()} "
                            f"| DELIVERY | {bucket.upper()} | {len(ordered)} stops | {hrs:.2f}h (+{current_hours:.1f}h → {projected_hours:.1f}h) | {km:.1f}km "
                            f"| Shift: {shift.id} → {shift.status}"
                        )

    logger.info("Route optimization completed successfully")

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
        status__in=[DriverShift.Status.ASSIGNED, DriverShift.Status.ACTIVE]
    )
    for shift in overdue_shifts:
        shift.status = DriverShift.Status.OVERDUE
        shift.save()
    logger.info(f"Marked {overdue_shifts.count()} shifts as overdue")
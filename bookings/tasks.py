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
        status=BookingStatus.SCHEDULED,
        pickup_address__latitude__isnull=False,
        dropoff_address__latitude__isnull=False,
        quote__service_type__isnull=False,
    ).exclude(
        Exists(Route.bookings.through.objects.filter(booking_id=OuterRef('pk')))
    ).annotate(service_bucket=service_case).select_related(
        'pickup_address', 'dropoff_address', 'quote', 'hub'
    )

    delivery_candidates = Booking.objects.filter(
        status=BookingStatus.AT_HUB,
        pickup_address__latitude__isnull=False,
        dropoff_address__latitude__isnull=False,
        quote__service_type__isnull=False,
    ).exclude(
        Exists(Route.bookings.through.objects.filter(booking_id=OuterRef('pk')))
    ).annotate(service_bucket=service_case).select_related(
        'pickup_address', 'dropoff_address', 'quote', 'hub'
    )

    total_candidates = pickup_candidates.count() + delivery_candidates.count()
    logger.info(f"Found {total_candidates} candidate bookings ({pickup_candidates.count()} pickups, {delivery_candidates.count()} deliveries)")

    if total_candidates == 0:
        logger.info("No eligible bookings for routing")
        return

    # ------------------------------------------------------------------
    # 2. Get hubs with coordinates
    # ------------------------------------------------------------------
    hubs = list(Hub.objects.select_related('address').filter(
        address__latitude__isnull=False,
        address__longitude__isnull=False
    ))

    if not hubs:
        logger.error("No hubs with coordinates!")
        return

    MAX_RADIUS_KM = 50

    # ------------------------------------------------------------------
    # 3. Group PICKUP legs by nearest hub (using pickup_address)
    # ------------------------------------------------------------------
    pickup_by_hub = defaultdict(list)
    for booking in pickup_candidates:
        distances = [
            (hub, great_circle(
                (booking.pickup_address.latitude, booking.pickup_address.longitude),
                (hub.address.latitude, hub.address.longitude)
            ).km)
            for hub in hubs
        ]
        nearest_hub, dist = min(distances, key=lambda x: x[1])
        if dist <= MAX_RADIUS_KM:
            pickup_by_hub[nearest_hub].append(booking)
        else:
            logger.warning(f"Booking {booking.id} pickup too far from any hub ({dist:.1f}km)")

    # ------------------------------------------------------------------
    # 4. Group DELIVERY legs by nearest hub (using dropoff_address)
    # ------------------------------------------------------------------
    delivery_by_hub = defaultdict(list)
    for booking in delivery_candidates:
        distances = [
            (hub, great_circle(
                (booking.dropoff_address.latitude, booking.dropoff_address.longitude),
                (hub.address.latitude, hub.address.longitude)
            ).km)
            for hub in hubs
        ]
        nearest_hub, dist = min(distances, key=lambda x: x[1])
        if dist <= MAX_RADIUS_KM:
            delivery_by_hub[nearest_hub].append(booking)
        else:
            logger.warning(f"Booking {booking.id} delivery too far from any hub ({dist:.1f}km)")

    # ------------------------------------------------------------------
    # 5. Process each hub-by-hub
    # ------------------------------------------------------------------
    for hub in hubs:
        logger.info(f"--- PROCESSING HUB: {hub.name} (ID: {hub.id}) ---")
        pickups = pickup_by_hub.get(hub, [])
        deliveries = delivery_by_hub.get(hub, [])
        logger.info(f"  Pickups: {len(pickups)} | Deliveries: {len(deliveries)}")

        # Drivers only from this hub + currently available 
        # Only drivers without active or assigned shifts
        available_drivers = list(
            DriverProfile.objects.filter(
                hub=hub,
                availability__available=True,
                user__is_active=True
            ).select_related('user', 'availability').annotate(
                has_open_shift=Exists(DriverShift.objects.filter(
                    driver=OuterRef('pk'), 
                    status__in=[DriverShift.Status.ASSIGNED, DriverShift.Status.ACTIVE]
                ))
            ).filter(has_open_shift=False) # Only drivers without open shifts
        )

        for d in available_drivers:
            DriverShift.get_or_create_today(d)

        logger.info(f"  Available drivers: {len(available_drivers)}")

        if not available_drivers and not pickups and not deliveries:
            continue

        # ------------------------------------------------------------------
        # 6. Process buckets (same_day → next_day → three_day)
        # ------------------------------------------------------------------
        buckets = ['same_day', 'next_day', 'three_day']
        legs = [
            ('pickup', pickups, 'pickup_address'),
            ('delivery', deliveries, 'dropoff_address')
        ]

        for bucket in buckets:
            for leg_type, bookings_list, addr_field in legs:
                bucket_bookings = [b for b in bookings_list if b.service_bucket == bucket]
                if not bucket_bookings:
                    continue

                logger.info(f"  → Optimizing {leg_type.upper()} | {bucket} | {len(bucket_bookings)} bookings")

                locations = [hub.address] + [getattr(b, addr_field) for b in bucket_bookings]
                time_matrix, distance_matrix = get_time_matrix(locations)

                # Optional time windows
                time_windows = None
                if leg_type == 'pickup' and bucket_bookings[0].scheduled_pickup_at:
                    time_windows = [(b.scheduled_pickup_at, b.scheduled_pickup_at + timedelta(hours=4)) for b in bucket_bookings]
                elif leg_type == 'delivery' and bucket_bookings[0].scheduled_dropoff_at:
                    time_windows = [(b.scheduled_dropoff_at - timedelta(hours=4), b.scheduled_dropoff_at) for b in bucket_bookings]

                routes = optimize_routes(
                    bookings=bucket_bookings,
                    hub_lat=hub.address.latitude,
                    hub_lng=hub.address.longitude,
                    time_matrix=time_matrix,
                    distance_matrix=distance_matrix,
                    drivers=available_drivers.copy(),  # copy so we don't exhaust the list across buckets
                    time_windows=time_windows,
                    leg_type=leg_type
                )

                for ordered, hrs, km, driver, etas in routes:
                    if not ordered:
                        continue

                    hrs = float(hrs)
                    km = float(km)

                    # Build ordered_stops early — used in both cases
                    ordered_stops = [
                        {
                            'booking_id': str(b.id),
                            'address': {
                                'lat': float(getattr(b, addr_field).latitude),
                                'lng': float(getattr(b, addr_field).longitude)
                            },
                            'eta': eta.isoformat() if eta else None
                        }
                        for b, eta in zip(ordered, etas)
                    ]

                    with transaction.atomic():
                        if not driver:
                            # ———————————————————————————————
                            # CASE 1: No driver available → Create PENDING shift & route
                            # ———————————————————————————————
                            pending_shift = DriverShift.objects.create(
                                driver=None,  # Explicitly null
                                start_time=now.replace(hour=8, minute=0, second=0, microsecond=0),  # Or your logic
                                end_time=now.replace(hour=18, minute=0, second=0, microsecond=0),
                                status=DriverShift.Status.PENDING,
                                current_load={'weight': 0.0, 'volume': 0.0, 'hours': 0.0},
                            )

                            route = Route.objects.create(
                                driver=None,
                                shift=pending_shift,
                                leg_type=leg_type,
                                ordered_stops=ordered_stops,
                                total_time_hours=round(hrs, 3),
                                total_distance_km=round(km, 3),
                                status='pending',
                                visible_at=now,
                            )
                            route.bookings.set(ordered)

                            # Keep bookings in SCHEDULED state until a driver is assigned
                            Booking.objects.filter(id__in=[b.id for b in ordered]).update(
                                hub=hub,
                                driver=None,
                                status=BookingStatus.SCHEDULED,
                                updated_at=now
                            )

                            logger.info(
                                f"PENDING ROUTE CREATED | Hub: {hub.name} | Driver: Unassigned "
                                f"| {leg_type.upper()} | {bucket.upper()} | {len(ordered)} stops | {hrs:.2f}h | {km:.1f}km "
                                f"| Shift: {pending_shift.id}"
                            )
                            continue  # Skip to next route

                        # ———————————————————————————————
                        # CASE 2: Driver found → Apply rules & assign
                        # ———————————————————————————————
                        shift = DriverShift.get_or_create_today(driver)

                        # ——— 10-hour daily limit check ———
                        current_hours = (shift.current_load or {}).get('hours', 0.0)
                        projected_hours = current_hours + hrs

                        if projected_hours > 10.0:
                            logger.info(
                                f"Skipping route – {driver.user.get_full_name()} would exceed 10h limit "
                                f"({projected_hours:.1f}h > 10.0h)"
                            )
                            continue

                        # ——— Minimum 8h route rule for non-same-day ———
                        if bucket != 'same_day' and hrs < DriverShift.MIN_ROUTE_HOURS:
                            logger.info(
                                f"Skipping small non-same_day route for {driver.user.get_full_name()} "
                                f"({hrs:.1f}h < {DriverShift.MIN_ROUTE_HOURS}h minimum)"
                            )
                            continue

                        # ——— All checks passed → Assign route ———
                        shift.status = DriverShift.Status.ASSIGNED
                        shift.current_load['hours'] = round(projected_hours, 2)
                        shift.save(update_fields=['status', 'current_load'])

                        route = Route.objects.create(
                            driver=driver,
                            shift=shift,
                            leg_type=leg_type,
                            ordered_stops=ordered_stops,
                            total_time_hours=round(hrs, 3),
                            total_distance_km=round(km, 3),
                            status='assigned',
                            visible_at=now,
                        )
                        route.bookings.set(ordered)

                        # Assign driver + hub + status to all bookings
                        Booking.objects.filter(id__in=[b.id for b in ordered]).update(
                            hub=hub,
                            driver=driver,
                            status=BookingStatus.ASSIGNED,
                            updated_at=now
                        )

                        logger.info(
                            f"ROUTE ASSIGNED | Hub: {hub.name} | Driver: {driver.user.get_full_name()} "
                            f"| {leg_type.upper()} | {bucket.upper()} | {len(ordered)} stops | {hrs:.2f}h (+{current_hours:.1f}h → {projected_hours:.1f}h) | {km:.1f}km "
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
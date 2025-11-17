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
from django.db.models import Exists, OuterRef, Prefetch, Case, When, F, FloatField, Value, CharField, IntegerField
from django.db.models.functions import Cast
from django.db import transaction
from dateutil.parser import parse


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

# ----------------------------------------------------------------------
# TIER TO DAYS MAPPING (for inferring deadlines)
# ----------------------------------------------------------------------
TIER_TO_DAYS = {
    'same_day': 0,      # Deadline: today
    'next_day': 1,      # Deadline: tomorrow
    'three_day': 3,     # Deadline: +3 days
}

# ----------------------------------------------------------------------
# CUTOFF FOR BOOKING TIME (e.g., 2 PM – after this, bump same_day to next_day)
# ----------------------------------------------------------------------
CUTOFF_HOUR = 14  # 2 PM – configurable

@shared_task
def optimize_bookings():
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)

    # ------------------------------------------------------------------
    # 1. BUILD SERVICE-TYPE ANNOTATION (using Case/When for efficiency)
    # ------------------------------------------------------------------
    service_type_case = Case(
        *[When(quote__service_type__name__iexact=key, then=Value(bucket))
          for key, bucket in SERVICE_TYPE_TO_BUCKET.items()],
        default=Value('three_day'),  # Fallback to lowest priority
        output_field=CharField()
    )

    # ------------------------------------------------------------------
    # 2. BUILD PRIORITY ANNOTATION (lower number = higher priority)
    # ------------------------------------------------------------------
    priority_case = Case(
        When(service_bucket='same_day', then=Value(0)),
        When(service_bucket='next_day', then=Value(1)),
        When(service_bucket='three_day', then=Value(2)),
        default=Value(2),  # Fallback
        output_field=IntegerField()
    )

    # ------------------------------------------------------------------
    # 3. FETCH ALL CANDIDATE BOOKINGS (with annotations)
    # ------------------------------------------------------------------
    pickups = Booking.objects.filter(
        status=BookingStatus.SCHEDULED,
        pickup_address__validated=True,
        pickup_address__latitude__isnull=False,
        quote__service_type__isnull=False,
    ).annotate(
        service_bucket=service_type_case,
        priority=priority_case
    ).exclude(
        Exists(Route.bookings.through.objects.filter(booking_id=OuterRef('pk')))
    ).order_by('priority', 'created_at')  # Prioritize urgent + older first

    deliveries = Booking.objects.filter(
        status=BookingStatus.AT_HUB,
        dropoff_address__validated=True,
        dropoff_address__latitude__isnull=False,
    ).annotate(
        service_bucket=service_type_case,
        priority=priority_case
    ).exclude(
        Exists(Route.bookings.through.objects.filter(booking_id=OuterRef('pk')))
    ).order_by('priority', 'created_at')

    logger.info(f"Total candidate pickups: {pickups.count()}")
    logger.info(f"Total candidate deliveries: {deliveries.count()}")

    # ------------------------------------------------------------------
    # 4. BUCKET BY TIER + CREATION TIME ADJUSTMENT
    #    - If same_day and created after cutoff, bump to next_day
    #    - Use created_at to infer urgency if no scheduled_at
    # ------------------------------------------------------------------
    same_day_pickups = []
    next_day_pickups = []
    three_day_pickups = []
    same_day_deliveries = []
    next_day_deliveries = []
    three_day_deliveries = []

    for b in pickups:
        bucket = b.service_bucket
        if bucket == 'same_day' and b.created_at.hour >= CUTOFF_HOUR:
            bucket = 'next_day'  # Bump if afternoon booking

        if bucket == 'same_day':
            same_day_pickups.append(b)
        elif bucket == 'next_day':
            next_day_pickups.append(b)
        else:
            three_day_pickups.append(b)

    for b in deliveries:
        bucket = b.service_bucket
        if bucket == 'same_day' and b.created_at.hour >= CUTOFF_HOUR:
            bucket = 'next_day'

        if bucket == 'same_day':
            same_day_deliveries.append(b)
        elif bucket == 'next_day':
            next_day_deliveries.append(b)
        else:
            three_day_deliveries.append(b)

    # ------------------------------------------------------------------
    # 5. LOG BUCKETS (for debugging)
    # ------------------------------------------------------------------
    logger.info(f"Same-day pickups: {len(same_day_pickups)}")
    logger.info(f"Next-day pickups: {len(next_day_pickups)}")
    logger.info(f"Three-day pickups: {len(three_day_pickups)}")
    logger.info(f"Same-day deliveries: {len(same_day_deliveries)}")
    logger.info(f"Next-day deliveries: {len(next_day_deliveries)}")
    logger.info(f"Three-day deliveries: {len(three_day_deliveries)}")

    # ------------------------------------------------------------------
    # 6. PER-HUB PROCESSING
    # ------------------------------------------------------------------
    hubs = Hub.objects.prefetch_related('address').all()

    for hub in hubs:
        logger.info(f"--- OPTIMIZING HUB: {hub.name} (ID: {hub.id}) ---")

        # DRIVERS: Fetch with active shifts
        drivers_qs = DriverProfile.objects.filter(
            hub=hub, status='active'
        ).select_related('availability').prefetch_related(
            Prefetch('shifts', queryset=DriverShift.objects.filter(
                start_time__lte=now, end_time__gte=now
            ), to_attr='active_shifts')
        )

        available_drivers = []
        for d in drivers_qs:
            shift = d.active_shifts[0] if d.active_shifts else DriverShift.get_or_create_today(d)
            avail = d.availability
            lat = avail.lat if avail and avail.lat else hub.address.latitude
            lng = avail.lng if avail and avail.lng else hub.address.longitude
            if shift.remaining_hours >= 0.5 and (avail and avail.available or not avail):
                available_drivers.append(d)

        logger.info(f"FINAL AVAILABLE DRIVERS: {len(available_drivers)}")

        # ------------------------------------------------------------------
        # HELPER: Process a leg (pickup/delivery) for a bucket
        # - Filters by hub distance
        # - Sorts by priority (already in QS, but reinforce)
        # - Infers time windows based on tier + created_at
        # ------------------------------------------------------------------
        def process_leg(bookings, leg_type, addr_field, bucket):
            if not bookings:
                return

            logger.info(f"Processing {leg_type} leg ({bucket}): {len(bookings)} bookings")

            # Build locations: [hub, booking1, booking2, ...]
            hub = Hub.objects.first()
            if not hub or not hub.address.latitude:
                logger.error("Hub missing or no coordinates")
                return

            locations = [hub.address] + [getattr(b, addr_field) for b in bookings]
            time_matrix, distance_matrix = get_time_matrix(locations)

            # Time windows: optional
            time_windows = None
            if leg_type == 'pickup':
                time_windows = [(b.scheduled_pickup_at, b.scheduled_pickup_at + timedelta(hours=4)) for b in bookings]
            elif leg_type == 'delivery':
                time_windows = [(b.scheduled_dropoff_at - timedelta(hours=4), b.scheduled_dropoff_at) for b in bookings]

            
            # Available drivers: filter via DriverAvailability (reverse OneToOne)
            # today = timezone.now().date()
            available_availabilities = DriverAvailability.objects.filter(
                # date=today,
                available=True,
                driver_profile__user__is_active=True
            ).select_related('driver_profile')

            available_drivers = []
            for avail in available_availabilities:
                driver = avail.driver_profile
                # This is now 100% safe — creates or returns existing shift
                DriverShift.get_or_create_today(driver)
                available_drivers.append(driver)

            logger.info(f"FINAL AVAILABLE DRIVERS: {len(available_drivers)}")

            routes = optimize_routes(
                bookings, hub.address.latitude, hub.address.longitude,
                time_matrix, distance_matrix, available_drivers,
                time_windows, leg_type
            )

            for ordered, hrs, km, driver, etas in routes:
                ordered_stops = [
                    {
                        'booking_id': str(b.id),
                        'address': {
                            'lat': float(getattr(b, addr_field).latitude),
                            'lng': float(getattr(b, addr_field).longitude)
                        },
                        'eta': eta.isoformat()
                    } for b, eta in zip(ordered, etas)
                ]

                route = Route.objects.create(
                    driver=driver,
                    leg_type=leg_type,
                    ordered_stops=ordered_stops,
                    total_time_hours=hrs,
                    total_distance_km=km,
                    status='assigned' if driver else 'pending'
                )
                route.bookings.set(ordered)

                if driver:
                    shift = DriverShift.get_or_create_today(driver)  # ← ALWAYS USE THIS
                    current = shift.current_load or {'hours': 0.0, 'weight': 0.0, 'volume': 0.0}
                    shift.current_load = {
                        'hours': float(current.get('hours', 0)) + hrs,
                        'weight': float(current.get('weight', 0)) + sum(float(b.quote.weight_kg) for b in ordered),
                        'volume': float(current.get('volume', 0)) + sum(float(b.quote.volume_m3) for b in ordered),
                    }
                    shift.save()

        # Process all buckets/legs (prioritize same_day first implicitly by order)
        process_leg(same_day_pickups, 'pickup', 'pickup_address', 'same_day')
        process_leg(same_day_deliveries, 'delivery', 'dropoff_address', 'same_day')
        process_leg(next_day_pickups, 'pickup', 'pickup_address', 'next_day')
        process_leg(next_day_deliveries, 'delivery', 'dropoff_address', 'next_day')
        process_leg(three_day_pickups, 'pickup', 'pickup_address', 'three_day')
        process_leg(three_day_deliveries, 'delivery', 'dropoff_address', 'three_day')

    logger.info("Optimization complete")

@shared_task
def send_route_email(route_id):
    route = Route.objects.get(id=route_id)
    driver_email = route.driver.user.email
    subject = f"Your Shift for {route.shift.start_time.date()}: Route Details"
    message = f"Route ID: {route.id}\nLeg: {route.leg_type}\nStops: {len(route.ordered_stops)}\nHours: {route.total_time_hours}\nDistance: {route.total_distance_km} km\nStatus: {route.status}"
    send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, [driver_email])
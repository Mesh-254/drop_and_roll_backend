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
from django.db.models import Exists, OuterRef, Prefetch, Case, When, F, FloatField
from django.db.models.functions import Cast
from django.db import transaction


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
# MAIN OPTIMIZATION TASK
# ----------------------------------------------------------------------
@shared_task
def optimize_bookings():
    """Automated task: Optimize and assign routes for all hubs."""
    now = timezone.now()
    hubs = Hub.objects.all()

    route_subquery = Route.bookings.through.objects.filter(booking_id=OuterRef('pk'))

    for hub in hubs:
        logger.info(f"--- OPTIMIZING HUB: {hub.name} (ID: {hub.id}) ---")

        # Driver fetching (as per previous fix)
        drivers_qs = DriverProfile.objects.filter(
            hub=hub,
            status='active',
        ).select_related('availability').annotate(
            has_shift=Exists(
                DriverShift.objects.filter(
                    driver=OuterRef('pk'),
                    start_time__lte=now,
                    end_time__gte=now
                )
            ),
            remaining_hours=Case(
                When(has_shift=True, then=Cast(F('shifts__max_hours'), FloatField()) - 
                    Cast(F('shifts__current_load__hours'), FloatField())),
                default=10.0,  # Updated to 10h
                output_field=FloatField()
            )
        )

        # Debug log every driver
        for d in drivers_qs:
            shift = d.shifts.filter(start_time__lte=now, end_time__gte=now).first()
            logger.info(
                f"Driver {d.user.email} | Available: {d.availability.available if d.availability else 'NO AVAIL'} | "
                f"Shift: {'YES' if shift else 'NO'} | "
                f"Remaining: {getattr(shift, 'remaining_hours', 10.0):.2f}h | "
                f"Lat/Lng: {d.availability.lat if d.availability else None}/{d.availability.lng if d.availability else None}"
            )

        available_drivers = []
        for d in drivers_qs:
            avail = d.availability
            if not avail or not avail.available or not avail.lat or not avail.lng:
                continue
            
            shift = d.shifts.filter(start_time__lte=now, end_time__gte=now).first()
            if not shift:
                logger.warning(f"Driver {d.user.email} has NO SHIFT → creating one")
                shift = DriverShift.get_or_create_today(d)
            
            remaining = shift.remaining_hours
            if remaining >= 0.5:  # Lenient threshold
                available_drivers.append(d)

        logger.info(f"FINAL AVAILABLE DRIVERS: {len(available_drivers)} → {[d.user.email for d in available_drivers]}")

        def process_leg(bookings, leg_type, address_field):
            if not bookings:
                logger.info(f"No {leg_type.upper()} bookings for hub {hub.id}")
                return

            # Enhanced log: Bookings by leg type
            booking_details = [(str(b.id), getattr(b, address_field).latitude, getattr(b, address_field).longitude) for b in bookings]
            logger.info(f"Fetched {len(bookings)} {leg_type.upper()} bookings for hub {hub.id}: IDs and coords {booking_details}")

            locations = [hub.address] + [getattr(b, address_field) for b in bookings]
            time_matrix, distance_matrix = get_time_matrix(locations)
            time_windows = [getattr(b, f'scheduled_{leg_type}_at') for b in bookings] if leg_type == 'pickup' else [b.scheduled_dropoff_at for b in bookings]

            try:
                routes = optimize_routes(bookings, hub.address.latitude, hub.address.longitude, time_matrix, distance_matrix, available_drivers, time_windows, leg_type)
            except Exception as e:
                logger.exception(f"Optimization failed for {leg_type}: {e}")
                routes = []

            # NEW: If no routes created (e.g., no drivers or VRP fail), use fallback
            if not routes and bookings:
                logger.warning(f"No routes from VRP for {leg_type} (drivers: {len(available_drivers)}) → Falling back to clustering without drivers")
                clusters = cluster_bookings(bookings, num_clusters=min(5, len(bookings) or 1))
                routes = []
                for cluster in clusters.values():
                    if not cluster:
                        continue
                    ordered = cluster
                    etas = [timezone.now() + timedelta(minutes=30 * (i + 1)) for i in range(len(ordered))]
                    
                    # SAFE: Convert all Decimals → float
                    total_distance_km = round(
                        sum(
                            float(distance(hub.address.latitude, hub.address.longitude,
                                        b.pickup_address.latitude, b.pickup_address.longitude))
                            for b in ordered
                        ),
                        3
                    )
                    
                    total_time_hours = round(len(ordered) * 0.5, 3)  # 30 min per stop

                    routes.append((ordered, total_time_hours, total_distance_km, None, etas))

            with transaction.atomic():
                for ordered, total_time_hours, total_distance_km, driver, etas in routes:
                    if not ordered:
                        continue

                    existing = Route.objects.filter(bookings__in=ordered).exists()
                    if existing:
                        logger.warning(f"Duplicate bookings in {leg_type}: skipping {[str(b.id) for b in ordered]}")
                        continue

                    ordered_stops = [
                        {
                            'booking_id': str(b.id),
                            'address': {
                                'lat': float(getattr(b, address_field).latitude),
                                'lng': float(getattr(b, address_field).longitude)
                            },
                            'eta': eta.isoformat() if eta else None
                        } for b, eta in zip(ordered, etas)
                    ]

                    route = Route.objects.create(
                        driver=driver,  # Can be None
                        leg_type=leg_type,
                        ordered_stops=ordered_stops,
                        total_time_hours=total_time_hours,
                        total_distance_km=total_distance_km,
                        status='pending' if driver is None else 'assigned'  # NEW: Pending if no driver
                    )
                    route.bookings.set(ordered)
                    logger.info(f"Created Route #{route.id} ({leg_type}) – {len(ordered)} stops, IDs {[str(b.id) for b in ordered]}, {total_time_hours:.3f}h, {total_distance_km:.3f}km (Driver: {driver.user.email if driver else 'NONE - Awaiting Admin'})")

                    if driver:
                        shift = driver.active_shifts[0]
                        shift.current_load['hours'] += total_time_hours
                        shift.current_load['weight'] += sum(float(b.quote.weight_kg) for b in ordered)
                        shift.current_load['volume'] += sum(b.quote.volume_m3 for b in ordered)
                        shift.save()
                        logger.info(f"Assigned to driver {driver.user.email} (remaining: {shift.remaining_hours:.3f}h)")

        # Pickups
        pickups_qs = Booking.objects.annotate(
            in_route=Exists(route_subquery)
        ).filter(
            status=BookingStatus.SCHEDULED,
            pickup_address__validated=True,
            pickup_address__latitude__isnull=False,
            in_route=False
        )
        pickups_for_hub = [
            b for b in pickups_qs
            if distance(hub.address.latitude, hub.address.longitude,
                        b.pickup_address.latitude, b.pickup_address.longitude) < 100
        ]
        process_leg(pickups_for_hub, 'pickup', 'pickup_address')

        # Deliveries
        deliveries_qs = Booking.objects.annotate(
            in_route=Exists(route_subquery)
        ).filter(
            status=BookingStatus.AT_HUB,
            hub=hub,
            dropoff_address__validated=True,
            dropoff_address__latitude__isnull=False,
            in_route=False
        )
        process_leg(deliveries_qs, 'delivery', 'dropoff_address')

    logger.info("Optimization complete")
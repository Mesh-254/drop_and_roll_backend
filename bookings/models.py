from __future__ import annotations

from decimal import Decimal

from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, EmailValidator, RegexValidator
from django.db import models
from django.utils import timezone
import uuid


class Hub(models.Model):
    name = models.CharField(max_length=100)
    address = models.OneToOneField(
        'Address', on_delete=models.PROTECT, related_name='hub')

    def __str__(self):
        return self.name


class BookingStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SCHEDULED = "scheduled", "Scheduled"
    ASSIGNED = "assigned", "Assigned"
    PICKED_UP = "picked_up", "Picked Up"
    AT_HUB = "at_hub", "At Hub"
    IN_TRANSIT = "in_transit", "In Transit"
    DELIVERED = "delivered", "Delivered"
    CANCELLED = "cancelled", "Cancelled"
    FAILED = "failed", "Failed"
    REFUNDED = "refunded", "Refunded"  # NEW: Post-delivery refunds/returns


class Address(models.Model):
    """Normalized address with optional geocoding fields."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    line1 = models.CharField(max_length=255)
    line2 = models.CharField(max_length=255, blank=True, null=True)
    city = models.CharField(max_length=120)
    region = models.CharField(max_length=120, blank=True, null=True)
    postal_code = models.CharField(max_length=20, blank=True, null=True)
    country = models.CharField(
        max_length=2, default="GB")  # ISO-3166-1 alpha-2
    latitude = models.DecimalField(
        max_digits=9, decimal_places=6, blank=True, null=True)
    longitude = models.DecimalField(
        max_digits=9, decimal_places=6, blank=True, null=True)
    validated = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["city", "region", "country"]),
        ]

    def __str__(self):
        return f"{self.line1}, {self.city} {self.postal_code or ''}".strip()


class ServiceType(models.Model):
    """Dynamic service types (replaces ServiceTier)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # e.g., "Standard Delivery", "Express 1-Hour"
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(
        blank=True, default="")  # e.g., "Same-Day/Next-Day"
    price = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00"))
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Service Type"
        verbose_name_plural = "Service Types"
        constraints = [models.UniqueConstraint(
            fields=["name"], name="unique_service_type_name")]

    def __str__(self):
        return self.name


class ShippingType(models.Model):
    """Dynamic shipping types (replaces ShipmentType)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # e.g., "Parcels", "Cargo"
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Shipping Type"
        verbose_name_plural = "Shipping Types"
        constraints = [models.UniqueConstraint(
            fields=["name"], name="unique_shipping_type_name")]

    def __str__(self):
        return self.name


class Quote(models.Model):
    """Snapshot of a computed quote for auditing and dispute resolution."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(default=timezone.now)

    distance_km = models.DecimalField(
        max_digits=7, decimal_places=2, validators=[MinValueValidator(0)])
    weight_kg = models.DecimalField(
        max_digits=6, decimal_places=2, validators=[MinValueValidator(0)])

    fragile = models.BooleanField(default=False)
    insurance_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0, blank=True, null=True, validators=[MinValueValidator(0)])
    dimensions = models.JSONField(default=dict, blank=True)

    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    surge_multiplier = models.DecimalField(
        max_digits=5, decimal_places=2, default=1)
    discount_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0)
    final_price = models.DecimalField(max_digits=10, decimal_places=2)

    shipping_type = models.ForeignKey(
        ShippingType, on_delete=models.SET_NULL, null=True, related_name="quotes")
    service_type = models.ForeignKey(
        ServiceType, on_delete=models.SET_NULL, null=True, related_name="quotes")

    # pricing breakdown/inputs
    meta = models.JSONField(default=dict, blank=True)

    # NEW: Computed volume in m³ from dimensions (assuming cm units)
    volume_m3 = models.FloatField(default=0.0)

    class Meta:
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=['service_type']),
        ]

    def save(self, *args, **kwargs):
        # NEW: Compute volume if dimensions provided (l/w/h in cm, convert to m³)
        if self.dimensions:
            l = self.dimensions.get('l', 0)
            w = self.dimensions.get('w', 0)
            h = self.dimensions.get('h', 0)
            self.volume_m3 = (l * w * h) / 1_000_000.0  # cm³ to m³
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.service_type.name if self.service_type else 'Unknown'} KES {self.final_price} ({self.distance_km}km, {self.weight_kg}kg)"

# NEW: Helper function to get volume (used in tasks)
def get_volume(booking):
    return booking.quote.volume_m3 if booking.quote else 0.0

class Booking(models.Model):
    """Single parcel delivery booking."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        null=True, blank=True, related_name="bookings")  # Allow null for anonymous users
    guest_identifier = models.CharField(
        max_length=255, blank=True, null=True, unique=True
    )  # Unique identifier for anonymous users
    guest_email = models.CharField(max_length=255, blank=True, null=True, validators=[
                                   EmailValidator()], db_index=True)

    driver = models.ForeignKey('driver.DriverProfile', on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="bookings")
    hub = models.ForeignKey(
        'Hub',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='hub_bookings',
        help_text="Hub where the booking is currently located (set when status='at_hub')."
    )

    pickup_address = models.ForeignKey(
        Address, on_delete=models.PROTECT, related_name="pickup_bookings", blank=True, null=True)
    dropoff_address = models.ForeignKey(
        Address, on_delete=models.PROTECT, related_name="dropoff_bookings", blank=True, null=True)

    status = models.CharField(
        max_length=20, choices=BookingStatus.choices, default=BookingStatus.PENDING)

    # Pricing snapshot copied from the accepted Quote
    quote = models.ForeignKey(
        Quote, on_delete=models.PROTECT, related_name="bookings")
    # Snapshot from quote
    final_price = models.DecimalField(max_digits=10, decimal_places=2)

    # Scheduling
    scheduled_pickup_at = models.DateTimeField(blank=True, null=True)
    scheduled_dropoff_at = models.DateTimeField(blank=True, null=True)

    # Audit fields
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    notes = models.TextField(blank=True, null=True)

    # Promo integration (from loyalty app; stored as a snapshot string code)
    promo_code = models.CharField(max_length=50, blank=True, null=True)
    discount_applied = models.DecimalField(
        max_digits=10, decimal_places=2, default=0)

    # New receiver contact fields
    receiver_email = models.CharField(
        max_length=255,
        validators=[EmailValidator(message="Enter a valid email address")],
        blank=True,  # Allow empty if optional
        null=True,   # Allow NULL in DB if optional
        help_text="Email address of the parcel receiver."
    )
    receiver_phone = models.CharField(
        max_length=20,
        validators=[
            RegexValidator(
                regex=r'^\+?1?\d{9,15}$',
                message="Phone number must be in a valid format, e.g., +1234567890 (9-15 digits)."
            )
        ],
        blank=True,
        null=True,
        help_text="Phone number of the parcel receiver (e.g., +1234567890)."
    )

    # prevent double payment and impotency
    payment_expires_at = models.DateTimeField(blank=True, null=True)
    payment_attempts = models.PositiveIntegerField(default=0)
    tracking_number = models.CharField(
        max_length=20, unique=True, blank=True, null=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["customer"]),
            models.Index(fields=['receiver_email']),
            models.Index(fields=["tracking_number"]),

        ]

    constraints = [
        models.CheckConstraint(
            check=models.Q(customer__isnull=False) | models.Q(
                guest_email__isnull=False),
            name="booking_must_have_customer_or_guest_email"
        )
    ]

    def __str__(self):
        return f"Booking {self.id} — {self.status}"


class RecurrencePeriod(models.TextChoices):
    WEEKLY = "weekly", "Weekly"
    MONTHLY = "monthly", "Monthly"


class RecurringSchedule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="recurring_schedules")

    # Either based on a quote OR an existing booking
    quote = models.ForeignKey(
        "Quote", on_delete=models.PROTECT, null=True, blank=True, related_name="recurring_schedules"
    )
    booking = models.ForeignKey(
        "Booking", on_delete=models.PROTECT, null=True, blank=True, related_name="recurring_schedules"
    )

    recurrence = models.CharField(
        max_length=20, choices=RecurrencePeriod.choices)
    next_run_at = models.DateTimeField()
    active = models.BooleanField(default=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Recurring {self.recurrence} for {self.customer_id}"

    def get_source(self):
        """Return whichever object this schedule is based on."""
        return self.booking or self.quote

    def resolve_fields(self):
        """Return a dict of all schedule-relevant fields from the source."""
        source = self.get_source()
        if isinstance(source, Booking):
            return {
                "pickup_address": source.pickup_address,
                "dropoff_address": source.dropoff_address,
                "weight_kg": source.quote.weight_kg,
                "service_type": source.quote.service_type,
            }
        elif isinstance(source, Quote):
            return {
                "pickup_address": None,  # You may require addresses if needed
                "dropoff_address": None,
                "weight_kg": source.weight_kg,
                "service_type": source.service_type,
            }
        return {}


class BulkUpload(models.Model):
    """CSV uploads for creating multiple bookings at once (B2B)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bulk_uploads")
    csv_file = models.FileField(upload_to="bulk_uploads/")
    created_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(blank=True, null=True)
    processed = models.BooleanField(default=False)
    # counts, errors per row
    result = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"BulkUpload {self.id} by {self.customer_id}"


class Route(models.Model):
    driver = models.ForeignKey(
        'driver.DriverProfile', on_delete=models.SET_NULL, null=True)
    bookings = models.ManyToManyField(Booking)  # Grouped bookings

    shift = models.ForeignKey('driver.DriverShift', on_delete=models.SET_NULL, null=True, related_name='routes')
    visible_at = models.DateTimeField(default=timezone.now)  # When driver can see it in dashboard

    leg_type = models.CharField(
        choices=[('pickup', 'Pickup'), ('delivery', 'Delivery')])
    # [{'booking_id': str(uuid), 'address': {'lat': float, 'lng': float}, 'eta': datetime}]
    ordered_stops = models.JSONField(default=list)
    total_time_hours = models.FloatField(default=0.0)
    total_distance_km = models.FloatField(default=0.0)
    status = models.CharField(choices=[('pending', 'Pending'), (
        'assigned', 'Assigned'), ('completed', 'Completed')], default='pending')
    
    # Add index for fast lookups
    class Meta:
        indexes = [models.Index(fields=["driver", "status"])]


# ADD Proof of delivery
class ProofOfDelivery(models.Model):
    pass

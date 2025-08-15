from __future__ import annotations
from django.db import models
from django.conf import settings
from django.utils import timezone
from django.core.validators import MinValueValidator
import uuid


class ServiceTier(models.TextChoices):
    STANDARD = "standard", "Standard"  # Same-Day/Next-Day
    EXPRESS = "express", "Express"      # 1–2 hour
    BUSINESS = "business", "Business"   # Scheduled (B2B)


class BookingStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SCHEDULED = "scheduled", "Scheduled"
    ASSIGNED = "assigned", "Assigned"
    PICKED_UP = "picked_up", "Picked Up"
    IN_TRANSIT = "in_transit", "In Transit"
    DELIVERED = "delivered", "Delivered"
    CANCELLED = "cancelled", "Cancelled"
    FAILED = "failed", "Failed"


class Address(models.Model):
    """Normalized address with optional geocoding fields."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    line1 = models.CharField(max_length=255)
    line2 = models.CharField(max_length=255, blank=True, null=True)
    city = models.CharField(max_length=120)
    region = models.CharField(max_length=120, blank=True, null=True)
    postal_code = models.CharField(max_length=20, blank=True, null=True)
    country = models.CharField(max_length=2, default="KE")  # ISO-3166-1 alpha-2
    latitude = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)
    validated = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["city", "region", "country"]),
        ]

    def __str__(self):
        return f"{self.line1}, {self.city} {self.postal_code or ''}".strip()


class Quote(models.Model):
    """Snapshot of a computed quote for auditing and dispute resolution."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(default=timezone.now)

    service_tier = models.CharField(max_length=20, choices=ServiceTier.choices)
    weight_kg = models.DecimalField(max_digits=6, decimal_places=2, validators=[MinValueValidator(0)])
    distance_km = models.DecimalField(max_digits=7, decimal_places=2, validators=[MinValueValidator(0)])

    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    surge_multiplier = models.DecimalField(max_digits=5, decimal_places=2, default=1)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    final_price = models.DecimalField(max_digits=10, decimal_places=2)

    meta = models.JSONField(default=dict, blank=True)  # pricing breakdown/inputs

    def __str__(self):
        return f"{self.service_tier} KES {self.final_price} ({self.distance_km}km, {self.weight_kg}kg)"


class Booking(models.Model):
    """Single parcel delivery booking."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="bookings")
    driver = models.ForeignKey("users.DriverProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="bookings")

    pickup_address = models.ForeignKey(Address, on_delete=models.PROTECT, related_name="pickup_bookings")
    dropoff_address = models.ForeignKey(Address, on_delete=models.PROTECT, related_name="dropoff_bookings")

    service_tier = models.CharField(max_length=20, choices=ServiceTier.choices)
    status = models.CharField(max_length=20, choices=BookingStatus.choices, default=BookingStatus.PENDING)

    weight_kg = models.DecimalField(max_digits=6, decimal_places=2, validators=[MinValueValidator(0)])
    distance_km = models.DecimalField(max_digits=7, decimal_places=2, validators=[MinValueValidator(0)])

    # Pricing snapshot copied from the accepted Quote
    quote = models.ForeignKey(Quote, on_delete=models.PROTECT, related_name="bookings")
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
    discount_applied = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    class Meta:
        indexes = [
            models.Index(fields=["status", "service_tier"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"Booking {self.id} — {self.status}"


class RecurrencePeriod(models.TextChoices):
    WEEKLY = "weekly", "Weekly"
    MONTHLY = "monthly", "Monthly"


class RecurringSchedule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="recurring_schedules")
    service_tier = models.CharField(max_length=20, choices=ServiceTier.choices)

    pickup_address = models.ForeignKey(Address, on_delete=models.PROTECT, related_name="recurring_pickups")
    dropoff_address = models.ForeignKey(Address, on_delete=models.PROTECT, related_name="recurring_dropoffs")

    weight_kg = models.DecimalField(max_digits=6, decimal_places=2, validators=[MinValueValidator(0)])
    recurrence = models.CharField(max_length=20, choices=RecurrencePeriod.choices)
    next_run_at = models.DateTimeField()
    active = models.BooleanField(default=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Recurring {self.recurrence} for {self.customer_id}"


class BulkUpload(models.Model):
    """CSV uploads for creating multiple bookings at once (B2B)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bulk_uploads")
    csv_file = models.FileField(upload_to="bulk_uploads/")
    created_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(blank=True, null=True)
    processed = models.BooleanField(default=False)
    result = models.JSONField(default=dict, blank=True)  # counts, errors per row

    def __str__(self):
        return f"BulkUpload {self.id} by {self.customer_id}"

from __future__ import annotations

import uuid

from datetime import timedelta
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone
from django.db import transaction

User = get_user_model()


class PayoutStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSED = "processed", "Processed"
    FAILED = "failed", "Failed"


class DriverProfile(models.Model):
    class Vehicle(models.TextChoices):
        BIKE = "bike", "Bike"
        CAR = "car", "Car"
        VAN = "van", "Van"
        TRUCK = "truck", "Truck"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        INACTIVE = "inactive", "Inactive"
        SUSPENDED = "suspended", "Suspended"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="driver_profile")
    license_number = models.CharField(max_length=50)
    vehicle_type = models.CharField(max_length=20, choices=Vehicle.choices)
    vehicle_registration = models.CharField(
        max_length=50, blank=True, null=True)
    is_verified = models.BooleanField(default=False)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.INACTIVE)

    # Performance
    total_deliveries = models.PositiveIntegerField(default=0)
    rating = models.DecimalField(max_digits=3, decimal_places=2, default=0.00)

    hub = models.ForeignKey('bookings.Hub', on_delete=models.SET_NULL, null=True, blank=True, related_name='drivers')
    
    max_weight_kg = models.DecimalField(max_digits=10, decimal_places=2, default=100.0)  # e.g., bike=10, truck=1000
    max_volume_m3 = models.DecimalField(max_digits=10, decimal_places=2, default=1.0)   # Add for dimensions

    def save(self, *args, **kwargs):
            super().save(*args, **kwargs)
            # Auto-create today's shift if missing
            DriverShift.get_or_create_today(self)

    def __str__(self):
            return f"DriverProfile({self.user.email}, {self.user.role})"
    

class DriverShift(models.Model):
    """
    Represents a single 8-hour shift for a driver.
    - start_time: When shift begins (e.g., 08:00)
    - end_time: When shift ends (start_time + 8h)
    - current_load: JSON dict tracking used capacity:
        {
            "weight": 45.5,     # kg used
            "volume": 0.8,     # m³ used
            "hours": 3.2       # hours of work assigned
        }
    - remaining_hours: Computed property
    """
    driver = models.ForeignKey(
        DriverProfile, on_delete=models.CASCADE, related_name='shifts')
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()  # Must be start_time + 8 hours
    max_hours = models.FloatField(default=8.0)
    current_load = models.JSONField(default=dict)

    max_hours = models.FloatField(default=8.0)  # Standardized to 8h
    MIN_ROUTE_HOURS = 4.0  # Configurable threshold for "short" routes

    def save(self, *args, **kwargs):
        # NEW: Initialize current_load with zeros if not set
        if not self.current_load:
            self.current_load = {'weight': 0.0, 'volume': 0.0, 'hours': 0.0}
        super().save(*args, **kwargs)

    @property
    def remaining_hours(self) -> float:
        """How many hours are still free in this shift."""
        used = self.current_load.get('hours', 0.0)
        return max(0.0, self.max_hours - used)

    def __str__(self):
        return f"{self.driver} {self.start_time.date()} ({self.start_time.strftime('%H:%M')} - {self.end_time.strftime('%H:%M')})"

    @classmethod
    def get_or_create_today(cls, driver):
        """Safely get or create today's shift. Handles duplicates and race conditions."""
        today = timezone.localtime(timezone.now()).date()
        start_time = timezone.make_aware(
            timezone.datetime.combine(today, timezone.datetime.min.time().replace(hour=8))
        )
        end_time = start_time + timedelta(hours=10)

        # Atomic block + select_for_update to prevent race conditions
        with transaction.atomic():
            shifts = cls.objects.select_for_update().filter(
                driver=driver,
                start_time__date=today
            )

            if shifts.exists():
                # If multiple (from past bug), return the first and delete extras
                shift = shifts.first()
                if shifts.count() > 1:
                    shifts.exclude(pk=shift.pk).delete()
                return shift

            # Create new
            shift = cls.objects.create(
                driver=driver,
                start_time=start_time,
                end_time=end_time,
                max_hours=10.0,
                current_load={'weight': 0.0, 'volume': 0.0, 'hours': 0.0}
            )
            return shift




class DriverDocument(models.Model):
    """KYC documents for drivers (license scan, insurance, national ID)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    driver = models.ForeignKey(
        DriverProfile, on_delete=models.CASCADE, related_name="documents")
    doc_type = models.CharField(max_length=50)  # e.g., license, insurance, id
    file = models.FileField(upload_to="drivers/docs/")
    uploaded_at = models.DateTimeField(default=timezone.now)
    verified = models.BooleanField(default=False)
    notes = models.TextField(blank=True, null=True)

    class Meta:
        # Ensure one doc_type per driver
        unique_together = ("driver", "doc_type")

    def __str__(self):
        return f"DriverDocument({self.doc_type}, {self.driver.user.email}, {self.driver.user.role})"


class DriverInvitation(models.Model):
    """Admin invites a driver; driver accepts and sets password via token."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        EXPIRED = "expired", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField()
    full_name = models.CharField(max_length=255)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="driver_invites_created")
    token = models.UUIDField(default=uuid.uuid4, unique=True)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(blank=True, null=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING)

    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    def save(self, *args, **kwargs):
        if self.is_expired() and self.status != self.Status.ACCEPTED:
            self.status = self.Status.EXPIRED
        super().save(*args, **kwargs)

    def __str__(self):
        return f"DriverInvitation({self.email})"



class DriverAvailability(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    driver_profile = models.OneToOneField(
        DriverProfile, on_delete=models.CASCADE, related_name="availability")
    available = models.BooleanField(default=False)
    lat = models.DecimalField(
        max_digits=9, decimal_places=6, blank=True, null=True)
    lng = models.DecimalField(
        max_digits=9, decimal_places=6, blank=True, null=True)
    last_updated = models.DateTimeField(default=timezone.now)


class DriverPayout(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    driver_profile = models.ForeignKey(
        DriverProfile, on_delete=models.CASCADE, related_name="payouts")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(
        max_length=16, choices=PayoutStatus.choices, default=PayoutStatus.PENDING)
    payout_date = models.DateTimeField(blank=True, null=True)
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)


class DriverRating(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    driver_profile = models.ForeignKey(
        DriverProfile, on_delete=models.CASCADE, related_name="ratings")
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="driver_ratings")
    # Optional link to booking (if bookings app is installed)
    booking = models.ForeignKey(
        "bookings.Booking", on_delete=models.SET_NULL, null=True, blank=True)
    rating = models.PositiveSmallIntegerField()
    comment = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("driver_profile", "customer", "booking")
